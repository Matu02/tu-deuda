"""Importación y consultas por lotes de cheques y deuda actual."""

from copy import deepcopy
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timezone
from io import BytesIO
import json
import math
import re
import threading

import pandas as pd

from bcra import checks_frame, debt_frame, fetch_endpoint, normalize_cuit
from bcra_errors import BCRACancelled, BCRACooldown, BCRAError, BCRARateLimit, BCRAUnavailable
from rate_policy import MAX_BATCH_WORKERS


def read_table(content: bytes, filename: str, sheet=0) -> pd.DataFrame:
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("El archivo supera los 20 MB permitidos.")
    if filename.lower().endswith(".xlsx"):
        return pd.read_excel(BytesIO(content), header=None, dtype=object, sheet_name=sheet, engine="openpyxl")
    if not filename.lower().endswith(".csv"):
        raise ValueError("Usá un archivo .xlsx o .csv.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            try:
                separator = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
            except csv.Error:
                separator = ","
            return pd.read_csv(BytesIO(content), header=None, dtype=object, sep=separator,
                               engine="python", encoding=encoding, skip_blank_lines=False)
        except UnicodeDecodeError:
            continue
    raise ValueError("No se pudo interpretar la codificación del CSV.")


def extract_cuits(table: pd.DataFrame, start_row=2, column=0):
    if not 1 <= start_row <= len(table) or not 0 <= column < len(table.columns):
        raise ValueError("La fila inicial o la columna están fuera del archivo.")
    cuits, invalid, seen, duplicates, blanks = [], [], set(), 0, 0
    for index in range(start_row - 1, len(table)):
        value = table.iloc[index, column]
        if pd.isna(value) or not str(value).strip():
            blanks += 1
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and float(value).is_integer():
            text = str(int(value))
        else:
            text = str(value).strip()
            if re.fullmatch(r"[0-9]{11}\.0+", text):
                text = text.split(".")[0]
        try:
            cuit = normalize_cuit(text)
        except ValueError as exc:
            invalid.append({"fila": index + 1, "valor": str(value), "motivo": str(exc)})
            continue
        if cuit in seen:
            duplicates += 1
        else:
            seen.add(cuit)
            cuits.append(cuit)
    return cuits, pd.DataFrame(invalid), duplicates, blanks


def checks_result(result: dict, rejection_date=None) -> dict:
    frame = checks_frame(result)
    invalid_dates = 0
    if not frame.empty:
        if "monto" not in frame:
            raise BCRAError("El detalle de cheques no incluye monto.")
        dates = pd.to_datetime(frame.get("fechaRechazo", pd.Series(index=frame.index, dtype="object")), errors="coerce", utc=True, format="mixed")
        invalid_dates = int(dates.isna().sum())
        if rejection_date:
            frame = frame.loc[dates.dt.strftime("%Y-%m-%d") == rejection_date].copy()
        amounts = pd.to_numeric(frame["monto"], errors="coerce")
        if amounts.isna().any() or not amounts.map(math.isfinite).all() or (amounts < 0).any():
            raise BCRAError("El BCRA devolvió un monto de cheque no válido.")
        frame["monto"] = amounts
    return {"estado": "parcial" if rejection_date and invalid_dates else "ok",
            "razon_social": result.get("denominacion") or "",
            "cheques": json.loads(frame.to_json(orient="records")),
            "fechas_invalidas": invalid_dates,
            "consultado": datetime.now(timezone.utc).isoformat()}


def debt_result(result):
    frame = debt_frame(None, result)
    if not frame.empty:
        amounts = pd.to_numeric(frame["monto"], errors="coerce")
        if amounts.isna().any() or not amounts.map(math.isfinite).all() or (amounts < 0).any():
            raise BCRAError("El BCRA devolvió un monto de deuda no válido.")
        if not frame["periodo"].map(lambda p: isinstance(p, str) and bool(re.fullmatch(r"[0-9]{4}(0[1-9]|1[0-2])", p))).all():
            raise BCRAError("El BCRA devolvió un período de deuda no válido.")
        frame["monto_ars"] = amounts * 1000
        if not frame["monto_ars"].map(math.isfinite).all():
            raise BCRAError("El monto de deuda está fuera de rango.")
    return {"estado": "ok", "razon_social": result.get("denominacion") or "",
            "deudas": json.loads(frame.to_json(orient="records", double_precision=15)),
            "consultado": datetime.now(timezone.utc).isoformat()}


def endpoint_status(record, kind):
    return record.get("estados_consulta", {}).get(kind, record.get("estado", "pendiente") if kind == "cheques" else "pendiente")


class BatchJob:
    """Hasta tres consultas en vuelo, sin llamadas a Streamlit desde trabajadores."""

    def __init__(self, cuits, target_date, records=None, rejection_date=None, analysis="cheques"):
        if analysis not in ("cheques", "deudas", "ambos"):
            raise ValueError("Tipo de análisis no válido.")
        self.analysis = analysis
        self.kinds = ("cheques", "actual") if analysis == "ambos" else (("actual",) if analysis == "deudas" else ("cheques",))
        self.cuits = list(dict.fromkeys(normalize_cuit(c) for c in cuits))
        self.target_date = date.fromisoformat(str(target_date)).isoformat()
        self.rejection_date = date.fromisoformat(rejection_date).isoformat() if rejection_date else None
        self.records = deepcopy(records or {})
        for record in self.records.values():
            record.setdefault("estados_consulta", {k: endpoint_status(record, k) for k in self.kinds})
            record.setdefault("errores_consulta", {k: record.get("error", "") for k in self.kinds})
            record.setdefault("deudas", [])
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.running = False
        self.message = "Lote preparado."
        self.thread = None
        self.retry_at = None

    def can_resume(self):
        return not self.retry_at or datetime.fromisoformat(self.retry_at) <= datetime.now(timezone.utc)

    def start(self):
        with self.lock:
            if self.running or not self.can_resume():
                return
            self.running = True
            self.retry_at = None
            self.stop_event.clear()
            self.message = "Consultando los análisis seleccionados…"
            self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.stop_event.set()

    def _run(self):
        consecutive_errors = 0
        pause_reason = None
        with self.lock:
            pending = deque((c, kind) for c in self.cuits for kind in self.kinds
                            if endpoint_status(self.records.get(c, {}), kind) != "ok")

        def worker():
            nonlocal consecutive_errors, pause_reason
            while True:
                with self.lock:
                    if self.stop_event.is_set() or not pending:
                        return
                    cuit, kind = pending.popleft()
                limited = False
                try:
                    result = fetch_endpoint(kind, cuit, stop_event=self.stop_event)
                    record = checks_result(result, self.rejection_date) if kind == "cheques" else debt_result(result)
                except BCRACancelled:
                    return
                except BCRAError as exc:
                    record = {"estado": "error", "error": str(exc), "cheques": [], "razon_social": ""}
                    limited = isinstance(exc, (BCRARateLimit, BCRACooldown))
                    if isinstance(exc, BCRAUnavailable):
                        with self.lock:
                            if exc.retry_at and (not self.retry_at or exc.retry_at > self.retry_at):
                                self.retry_at = exc.retry_at
                except Exception:
                    record = {"estado": "error", "error": "No se pudo interpretar la respuesta del BCRA.", "cheques": [], "razon_social": ""}
                with self.lock:
                    previous = self.records.get(cuit, {})
                    states = {k: endpoint_status(previous, k) for k in self.kinds}
                    states[kind] = record["estado"]
                    errors = dict(previous.get("errores_consulta", {}))
                    errors[kind] = record.get("error", "")
                    merged = {**previous, "cheques": previous.get("cheques", []), "deudas": previous.get("deudas", [])}
                    field = "cheques" if kind == "cheques" else "deudas"
                    merged[field] = record.get(field, [])
                    if record.get("razon_social"):
                        merged["razon_social"] = record["razon_social"]
                    merged.setdefault("razon_social", "")
                    if kind == "cheques":
                        merged["fechas_invalidas"] = record.get("fechas_invalidas", 0)
                    merged.update(estados_consulta=states, errores_consulta=errors,
                                  estado="ok" if all(v == "ok" for v in states.values()) else
                                  ("parcial" if any(v in ("ok", "parcial") for v in states.values()) else "error"),
                                  error="; ".join(f"{k}: {v}" for k, v in errors.items() if v),
                                  consultado=record.get("consultado", previous.get("consultado")))
                    self.records[cuit] = merged
                    consecutive_errors = consecutive_errors + 1 if record["estado"] == "error" else 0
                    if limited or consecutive_errors >= 5:
                        pause_reason = record["error"] if limited else "Lote pausado tras 5 errores consecutivos. Revisá el servicio y reanudá más tarde."
                        self.message = pause_reason
                        self.stop_event.set()

        try:
            with ThreadPoolExecutor(max_workers=MAX_BATCH_WORKERS) as pool:
                futures = [pool.submit(worker) for _ in range(MAX_BATCH_WORKERS)]
                for future in futures:
                    future.result()
            with self.lock:
                self.message = pause_reason or ("Lote pausado. Podés continuarlo." if self.stop_event.is_set() else "Consultas finalizadas. Revisá los errores o resultados parciales, si los hay.")
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        with self.lock:
            return {"version": 4, "analisis": self.analysis, "fecha_rechazo": self.rejection_date, "fecha": self.target_date, "cuits": self.cuits.copy(),
                    "resultados": deepcopy(self.records), "running": self.running, "mensaje": self.message, "reintentar_desde": self.retry_at}


def restore_job(content: bytes) -> BatchJob:
    """Importar sólo JSON validado; nunca ejecutar código del archivo de avance."""
    try:
        if len(content) > 100 * 1024 * 1024:
            raise ValueError("Archivo de avance demasiado grande.")
        data = json.loads(content)
        if data["version"] not in (1, 2, 3, 4) or not isinstance(data["cuits"], list) or not isinstance(data["resultados"], dict):
            raise ValueError("Formato de avance no válido.")
        target = date.fromisoformat(data["fecha"]).isoformat()
        cuits = list(dict.fromkeys(normalize_cuit(c) for c in data["cuits"]))
        for cuit, record in data["resultados"].items():
            if cuit not in cuits or record["estado"] not in ("ok", "parcial", "error") or not isinstance(record["cheques"], list):
                raise ValueError("Resultados no válidos.")
            if not isinstance(record.get("razon_social"), str):
                raise ValueError("Razón social no válida.")
            if not isinstance(record.get("error", ""), str) or not isinstance(record.get("fechas_invalidas", 0), int) or record.get("fechas_invalidas", 0) < 0:
                raise ValueError("Estado de consulta no válido.")
            for check in record["cheques"]:
                if not isinstance(check, dict) or not {"monto", "entidad", "causal"} <= check.keys():
                    raise ValueError("Detalle de cheques incompleto.")
                amount = check["monto"]
                if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                    raise ValueError("Detalle de cheques no válido.")
        analysis = data.get("analisis") if data["version"] == 4 else "cheques"
        if analysis not in ("cheques", "deudas", "ambos"):
            raise ValueError("Análisis no válido.")
        if data["version"] == 4:
            kinds = {"cheques", "actual"} if analysis == "ambos" else ({"actual"} if analysis == "deudas" else {"cheques"})
            for record in data["resultados"].values():
                states = record["estados_consulta"]
                errors = record["errores_consulta"]
                if not isinstance(states, dict) or set(states) != kinds or any(v not in ("ok", "parcial", "error", "pendiente") for v in states.values()):
                    raise ValueError("Estados de endpoints no válidos.")
                if not isinstance(errors, dict) or not set(errors) <= kinds or any(not isinstance(v, str) for v in errors.values()):
                    raise ValueError("Errores de endpoints no válidos.")
                if not isinstance(record.get("deudas"), list):
                    raise ValueError("Deudas no válidas.")
                for debt in record["deudas"]:
                    if not isinstance(debt, dict) or not {"monto", "monto_ars", "periodo", "entidad"} <= debt.keys():
                        raise ValueError("Detalle de deuda incompleto.")
                    amount = debt["monto"]
                    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0 or not math.isclose(debt["monto_ars"], amount * 1000, rel_tol=1e-12, abs_tol=1e-7) or not math.isfinite(debt["monto_ars"]):
                        raise ValueError("Monto de deuda no válido.")
                    if not isinstance(debt["periodo"], str) or not re.fullmatch(r"[0-9]{4}(0[1-9]|1[0-2])", debt["periodo"]):
                        raise ValueError("Período no válido.")
        legacy = data["version"] == 1
        if legacy:
            for record in data["resultados"].values():
                if record["estado"] == "ok":
                    record["estado"] = "parcial"
                    record.pop("estados_consulta", None)
                    record.pop("errores_consulta", None)
                    record["error"] = "Consulta anterior filtrada por fecha: requiere nueva consulta completa."
        job = BatchJob(cuits, target, records=data["resultados"], rejection_date=data.get("fecha_rechazo") if data["version"] >= 3 else None, analysis=analysis)
        if legacy:
            job.message = "Avance anterior filtrado por fecha. Al continuar, se volverán a consultar todos los CUIT para obtener los resultados completos."
        retry_at = data.get("reintentar_desde")
        if retry_at is not None:
            parsed = datetime.fromisoformat(retry_at)
            if parsed.tzinfo is None:
                raise ValueError("El plazo debe incluir zona horaria.")
            job.retry_at = parsed.astimezone(timezone.utc).isoformat()
            job.message = str(BCRAUnavailable(job.retry_at))
        return job
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("El archivo de avance no tiene un formato válido de Tu Deuda.") from exc


def report_frames(snapshot, entities=None):
    rows, summary = [], []
    for cuit in snapshot["cuits"]:
        record = snapshot["resultados"].get(cuit)
        checks = record.get("cheques", []) if record else []
        name = record.get("razon_social", "") if record else ""
        observed = record is not None and endpoint_status(record, "cheques") in ("ok", "parcial")
        for check in checks:
            rows.append({**check, "CUIT": cuit, "razon_social": name})
        summary.append({"CUIT": cuit, "razon_social": name,
                        "estado": record["estado"] if record else "pendiente",
                        **{f"estado_{label}": endpoint_status(record or {}, k) if snapshot.get("analisis", "cheques") in modes else "no solicitado"
                           for k, label, modes in (("cheques", "cheques", ("cheques", "ambos")), ("actual", "deudas", ("deudas", "ambos")))},
                        "cantidad_cheques": len(checks) if observed else None,
                        "monto_total": sum(c["monto"] for c in checks) if observed else None,
                        "fechas_invalidas": record.get("fechas_invalidas", 0) if record else 0,
                        "error": record.get("error", "") if record else ""})
    frame = pd.json_normalize(rows)
    if not frame.empty:
        if entities is not None:
            codes = pd.to_numeric(frame["entidad"], errors="coerce")
            fallback = frame["entidad"].map(lambda c: f"Entidad no identificada (código {c})")
            frame["nombreEntidad"] = codes.map(entities).fillna(fallback)
        first = ["CUIT", "razon_social"]
        frame = frame[first + [c for c in frame if c not in first]]
    return frame, pd.DataFrame(summary)


def debt_report_frames(snapshot):
    rows, summary = [], []
    for cuit in snapshot["cuits"]:
        record = snapshot["resultados"].get(cuit, {})
        name = record.get("razon_social", "")
        debts = record.get("deudas", [])
        state = endpoint_status(record, "actual")
        periods = sorted({d["periodo"] for d in debts})
        latest = periods[-1] if periods else None
        for debt in debts:
            rows.append({**debt, "CUIT": cuit, "razon_social": name})
        summary.append({"CUIT": cuit, "razon_social": name, "estado": state,
                        "periodo": latest,
                        "deuda_ars": sum(d["monto_ars"] for d in debts if d["periodo"] == latest) if state == "ok" else None,
                        "error": record.get("errores_consulta", {}).get("actual", "")})
    return pd.DataFrame(rows), pd.DataFrame(summary)
