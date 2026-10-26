"""Importación y trabajo por lotes. Nunca consulta deudas ni historial."""

from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from io import BytesIO
import json
import math
import re
import threading

import pandas as pd

from bcra import BCRACancelled, BCRAError, BCRARateLimit, checks_frame, fetch_endpoint, normalize_cuit


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


def daily_result(result: dict, target_date: str) -> dict:
    frame = checks_frame(result)
    invalid_dates = 0
    if not frame.empty:
        if "fechaRechazo" not in frame or "monto" not in frame:
            raise BCRAError("El detalle de cheques no incluye fechaRechazo o monto.")
        dates = pd.to_datetime(frame["fechaRechazo"], errors="coerce", utc=True, format="mixed")
        invalid_dates = int(dates.isna().sum())
        frame = frame.loc[dates.dt.strftime("%Y-%m-%d") == target_date].copy()
        amounts = pd.to_numeric(frame["monto"], errors="coerce")
        if amounts.isna().any() or not amounts.map(math.isfinite).all() or (amounts < 0).any():
            raise BCRAError("El BCRA devolvió un monto de cheque no válido.")
        frame["monto"] = amounts
    return {"estado": "parcial" if invalid_dates else "ok",
            "razon_social": result.get("denominacion") or "Razón social no informada",
            "cheques": json.loads(frame.to_json(orient="records")),
            "fechas_invalidas": invalid_dates,
            "consultado": datetime.now(timezone.utc).isoformat()}


class BatchJob:
    """Un hilo secuencial por lote, sin llamadas a Streamlit desde el trabajador."""

    def __init__(self, cuits, target_date, interval=2.0, records=None):
        self.cuits = list(dict.fromkeys(normalize_cuit(c) for c in cuits))
        self.target_date = date.fromisoformat(str(target_date)).isoformat()
        self.interval = max(1.0, float(interval))
        self.records = deepcopy(records or {})
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.running = False
        self.message = "Lote preparado."
        self.thread = None

    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.stop_event.clear()
            self.message = "Consultando cheques rechazados…"
            self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.stop_event.set()

    def _run(self):
        consecutive_errors = 0
        try:
            for cuit in self.cuits:
                with self.lock:
                    if self.records.get(cuit, {}).get("estado") == "ok":
                        continue
                if self.stop_event.is_set():
                    break
                limited = False
                try:
                    result = fetch_endpoint("cheques", cuit, stop_event=self.stop_event)
                    record = daily_result(result, self.target_date)
                    consecutive_errors = 0
                except BCRACancelled:
                    break
                except BCRAError as exc:
                    record = {"estado": "error", "error": str(exc), "cheques": [], "razon_social": ""}
                    consecutive_errors += 1
                    limited = isinstance(exc, BCRARateLimit)
                except Exception:
                    record = {"estado": "error", "error": "No se pudo interpretar la respuesta de cheques.", "cheques": [], "razon_social": ""}
                    consecutive_errors += 1
                with self.lock:
                    self.records[cuit] = record
                    if limited or consecutive_errors >= 5:
                        self.message = record["error"] if limited else "Lote pausado tras 5 errores consecutivos. Revisá el servicio y reanudá más tarde."
                if limited or consecutive_errors >= 5:
                    return
                if self.stop_event.wait(self.interval):
                    break
            with self.lock:
                self.message = "Lote pausado. Podés continuarlo." if self.stop_event.is_set() else "Consultas finalizadas. Revisá los errores o resultados parciales, si los hay."
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        with self.lock:
            return {"version": 1, "fecha": self.target_date, "cuits": self.cuits.copy(),
                    "resultados": deepcopy(self.records), "running": self.running, "mensaje": self.message}


def restore_job(content: bytes) -> BatchJob:
    """Importar sólo JSON validado; nunca ejecutar código del archivo de avance."""
    try:
        if len(content) > 100 * 1024 * 1024:
            raise ValueError("Archivo de avance demasiado grande.")
        data = json.loads(content)
        if data["version"] != 1 or not isinstance(data["cuits"], list) or not isinstance(data["resultados"], dict):
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
                if not isinstance(check, dict) or not {"monto", "fechaRechazo", "entidad", "causal"} <= check.keys():
                    raise ValueError("Detalle de cheques incompleto.")
                amount = check["monto"]
                if str(check["fechaRechazo"])[:10] != target or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                    raise ValueError("Detalle de cheques no válido.")
        return BatchJob(cuits, target, records=data["resultados"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("El archivo de avance no tiene un formato válido de Tu Deuda.") from exc


def report_frames(snapshot, entities=None):
    rows, summary = [], []
    for cuit in snapshot["cuits"]:
        record = snapshot["resultados"].get(cuit)
        checks = record.get("cheques", []) if record else []
        name = record.get("razon_social", "") if record else ""
        observed = record is not None and record["estado"] in ("ok", "parcial")
        for check in checks:
            rows.append({**check, "CUIT": cuit, "razon_social": name})
        summary.append({"CUIT": cuit, "razon_social": name,
                        "estado": record["estado"] if record else "pendiente",
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
