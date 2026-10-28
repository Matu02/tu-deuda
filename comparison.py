"""Comparación local de dos extracciones de cheques, sin consultas a la API."""

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import math

import pandas as pd

from bcra import normalize_cuit
from bulk import endpoint_status, read_table


IDENTITY = ("CUIT", "entidad", "nroCheque", "fechaRechazo")
REQUIRED = (*IDENTITY, "monto")
POSSIBLE_PAYMENT = "Posible pago: dejó de figurar"
UNCHANGED = "Sigue informado"
NEW = "Nuevo en la extracción actual"
INCOMPARABLE = "No comparable"


def read_previous(content, filename, sheet=0):
    table = read_table(content, filename, sheet)
    if table.empty:
        raise ValueError("El archivo está vacío.")
    headers = [str(v).strip() for v in table.iloc[0]]
    if len(headers) != len(set(headers)):
        raise ValueError("El archivo tiene encabezados duplicados.")
    missing = set(REQUIRED) - set(headers)
    if missing:
        raise ValueError("Subí el detalle de «Todos los cheques». Faltan columnas: " + ", ".join(sorted(missing)))
    frame = table.iloc[1:].copy()
    frame.columns = headers
    return frame.dropna(how="all").reset_index(drop=True)


def _text(value):
    return "" if value is None or pd.isna(value) else str(value).strip()


def _integer(value):
    try:
        number = Decimal(_text(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            return ""
        return str(int(number))
    except (InvalidOperation, ValueError):
        return ""


def _day(value):
    if not _text(value):
        return ""
    try:
        # Las descargas de Tu Deuda usan fechas ISO; no interpretar números
        # ni fechas locales ambiguas como identificadores de cheques.
        return date.fromisoformat(_text(value)[:10]).isoformat()
    except ValueError:
        return ""


def _prepare(frame):
    records = []
    for original in frame.to_dict("records"):
        row = dict(original)
        try:
            text = _text(row.get("CUIT"))
            row["CUIT"] = normalize_cuit(text if "-" in text else _integer(text))
        except ValueError:
            row["CUIT"] = ""
        row["entidad"] = _integer(row.get("entidad"))
        row["nroCheque"] = _integer(row.get("nroCheque"))
        row["fechaRechazo"] = _day(row.get("fechaRechazo"))
        key = tuple(row[k] for k in IDENTITY)
        row["_key"] = key if all(key) else None
        row["_original"] = original
        try:
            amount = float(row.get("monto"))
            if not math.isfinite(amount) or amount < 0:
                raise ValueError
            row["monto"] = amount
            row["_amount_ok"] = True
        except (ValueError, TypeError):
            row["monto"] = None
            row["_amount_ok"] = False
        records.append(row)
    return records


def compare_checks(previous, current, snapshot, previous_date):
    """Devuelve detalle por cheque y resumen de posibles pagos por empresa."""
    previous_date = date.fromisoformat(str(previous_date))
    current_date = date.fromisoformat(snapshot["fecha"])
    if previous_date >= current_date:
        raise ValueError("La fecha anterior debe ser previa a la extracción actual.")
    before, after = _prepare(previous), _prepare(current)
    before_counts = Counter(r["_key"] for r in before if r["_key"])
    after_counts = Counter(r["_key"] for r in after if r["_key"])
    unique_after = {r["_key"]: (i, r) for i, r in enumerate(after)
                    if r["_key"] and after_counts[r["_key"]] == 1}
    uncertain_current = {r["CUIT"] for r in after if not r["_key"]}
    used, output = set(), []

    def emit(old, new, result, reason=""):
        row = old if old is not None else new
        original = row["_original"]
        paid = old is not None and bool(_text(old.get("fechaPago")))
        rejection = row["fechaRechazo"]
        age = (current_date - date.fromisoformat(rejection)).days if rejection else None
        output.append({
            **{f"anterior.{k}": v for k, v in (old or {}).get("_original", {}).items()},
            **{f"actual.{k}": v for k, v in (new or {}).get("_original", {}).items()},
            **{k: row.get(k) or original.get(k) for k in IDENTITY},
            "razon_social": _text((new or {}).get("razon_social")) or _text((old or {}).get("razon_social")),
            "nombreEntidad": _text(original.get("nombreEntidad")),
            "monto_anterior": old["monto"] if old is not None else None,
            "monto_actual": new["monto"] if new is not None else None,
            "fecha_extraccion_anterior": previous_date.isoformat(),
            "fecha_extraccion_actual": current_date.isoformat(),
            "antiguedad_dias": age,
            "fechaPago_anterior": _text(old.get("fechaPago")) if old is not None else "",
            "pagado_previamente": paid,
            "resultado": result,
            "motivo": reason,
            "nuevo_posible_pago": result == POSSIBLE_PAYMENT and not paid,
        })

    def issue(row):
        if not row["_key"]:
            return "Identidad del cheque incompleta o no válida."
        if before_counts[row["_key"]] > 1 or after_counts[row["_key"]] > 1:
            return "Identidad duplicada ambigua."
        if not row["_amount_ok"]:
            return "Monto no válido."
        cuit = row["CUIT"]
        if cuit not in snapshot["cuits"]:
            return "Empresa no incluida en el lote actual."
        if endpoint_status(snapshot["resultados"].get(cuit, {}), "cheques") != "ok":
            return "Consulta actual de cheques pendiente, parcial o con error."
        target = snapshot.get("fecha_rechazo")
        if target and row["fechaRechazo"] != target:
            return "Fuera del filtro de fecha de rechazo de la consulta actual."
        return ""

    for old in before:
        candidate = unique_after.get(old["_key"])
        new = None
        if candidate is not None and before_counts[old["_key"]] == 1:
            index, new = candidate
            used.add(index)
        reason = issue(old) or (issue(new) if new is not None else "")
        if not reason and new is None and old["CUIT"] in uncertain_current:
            reason = "La extracción actual contiene cheques sin identidad completa para esta empresa."
        emit(old, new, INCOMPARABLE if reason else (UNCHANGED if new is not None else POSSIBLE_PAYMENT), reason)
    for index, new in enumerate(after):
        if index not in used:
            reason = issue(new)
            emit(None, new, INCOMPARABLE if reason else NEW, reason)

    columns = [*IDENTITY, "razon_social", "nombreEntidad", "monto_anterior", "monto_actual",
               "fecha_extraccion_anterior", "fecha_extraccion_actual", "antiguedad_dias",
               "fechaPago_anterior", "pagado_previamente", "resultado", "motivo", "nuevo_posible_pago"]
    detail = pd.DataFrame(output) if output else pd.DataFrame(columns=columns)
    detail = detail[columns + [c for c in detail if c not in columns]]
    summary = (detail.loc[detail["nuevo_posible_pago"] == True]
               .groupby(["CUIT", "razon_social"], dropna=False, as_index=False)
               .agg(cantidad_posibles_pagos=("resultado", "size"), monto_posibles_pagos=("monto_anterior", "sum")))
    return detail, summary
