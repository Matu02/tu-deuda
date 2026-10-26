"""Cliente público de la Central de Deudores y normalización de sus respuestas."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re
import threading
import time
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

BASE_URL = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
ENTITIES_URL = "https://api.bcra.gob.ar/cheques/v1.0/entidades"
ENDPOINTS = {"actual": "", "historicas": "Historicas/", "cheques": "ChequesRechazados/"}


class BCRAError(Exception):
    """Error consultable sin exponer detalles internos de red."""


class BCRARateLimit(BCRAError):
    """El servidor sigue limitando las solicitudes luego de los reintentos."""


class BCRACancelled(BCRAError):
    """Consulta pausada por el usuario."""


class RequestGate:
    """Ritmo compartido por todos los usuarios e hilos de este proceso."""

    def __init__(self, interval=1.0):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_request = 0.0

    def defer(self, seconds):
        with self.lock:
            self.next_request = max(self.next_request, time.monotonic() + seconds)

    def wait(self, stop_event=None):
        while True:
            if stop_event is not None and stop_event.is_set():
                raise BCRACancelled("Consulta pausada.")
            with self.lock:
                delay = self.next_request - time.monotonic()
                if delay <= 0:
                    self.next_request = time.monotonic() + self.interval
                    return
            if stop_event is not None:
                stop_event.wait(min(delay, 0.5))
            else:
                time.sleep(min(delay, 0.5))


_REQUEST_GATE = RequestGate()


def retry_after_seconds(value):
    """Retry-After admite segundos o una fecha HTTP."""
    try:
        seconds = float(value)
        return max(0, seconds) if pd.notna(seconds) and seconds != float("inf") else 60
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60


def normalize_cuit(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"(?:[0-9]{11}|[0-9]{2}-[0-9]{8}-[0-9])", value):
        raise ValueError("Ingresá un CUIT de 11 dígitos, con o sin guiones.")
    digits = value.replace("-", "")
    check = 11 - sum(int(n) * w for n, w in zip(digits[:10], [5, 4, 3, 2, 7, 6, 5, 4, 3, 2])) % 11
    check = 0 if check == 11 else 9 if check == 10 else check
    if check != int(digits[-1]):
        raise ValueError("El dígito verificador del CUIT no es válido.")
    return digits


def _get_payload(url: str, allow_not_found: bool = False, stop_event=None) -> dict:
    try:
        with requests.Session() as session:
            for attempt in range(3):
                _REQUEST_GATE.wait(stop_event)
                response = session.get(url, timeout=(8, 30))
                if response.status_code == 429:
                    _REQUEST_GATE.defer(max(2 ** attempt, retry_after_seconds(response.headers.get("Retry-After"))))
                    if attempt == 2:
                        raise BCRARateLimit("El BCRA limitó las solicitudes (HTTP 429). El lote se pausó; reanudalo más tarde.")
                elif response.status_code in (500, 502, 503, 504) and attempt < 2:
                    _REQUEST_GATE.defer(2 ** attempt)
                else:
                    break
        if response.status_code == 404 and allow_not_found:
            return {"status": 404}
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.SSLError as exc:
        raise BCRAError("No se pudo verificar el certificado HTTPS del BCRA.") from exc
    except requests.exceptions.Timeout as exc:
        raise BCRAError("El BCRA demoró demasiado en responder. Intentá nuevamente.") from exc
    except requests.exceptions.RequestException as exc:
        raise BCRAError("No se pudo consultar al BCRA. Intentá nuevamente en unos minutos.") from exc
    except ValueError as exc:
        raise BCRAError("El BCRA devolvió una respuesta que no es JSON válido.") from exc
    if not isinstance(payload, dict):
        raise BCRAError("La respuesta del BCRA tiene un formato inesperado.")
    return payload


def fetch_entities() -> dict[int, str]:
    """Catálogo público de entidades habilitadas para operar con cheques."""
    payload = _get_payload(ENTITIES_URL)
    records = payload.get("results")
    if payload.get("status") != 200 or not isinstance(records, list) or not records:
        raise BCRAError("El catálogo de entidades del BCRA no está disponible.")
    try:
        catalog = {}
        for record in records:
            name = record["denominacion"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Denominación vacía")
            catalog[int(record["codigoEntidad"])] = name
        return catalog
    except (KeyError, TypeError, ValueError) as exc:
        raise BCRAError("El catálogo de entidades del BCRA tiene un formato inesperado.") from exc


def fetch_endpoint(kind: str, cuit: str, stop_event=None) -> dict:
    cuit = normalize_cuit(cuit)
    payload = _get_payload(f"{BASE_URL}/{ENDPOINTS[kind]}{cuit}", allow_not_found=True, stop_event=stop_event)
    if payload.get("status") == 404:
        return {}
    if payload.get("status") != 200 or not isinstance(payload.get("results"), dict):
        raise BCRAError("El BCRA no pudo completar esta consulta.")
    result = payload["results"]
    collection = "causales" if kind == "cheques" else "periodos"
    if not isinstance(result.get(collection), list):
        raise BCRAError("La respuesta del BCRA no contiene el detalle esperado.")
    return result


def fetch_report(cuit: str) -> dict:
    """Consultar en paralelo, conservando las secciones que sí respondieron."""
    cuit = normalize_cuit(cuit)
    report = {"cuit": cuit, "consultado": datetime.now(timezone.utc).isoformat(), "errores": {}}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {kind: pool.submit(fetch_endpoint, kind, cuit) for kind in ENDPOINTS}
        for kind, future in futures.items():
            try:
                report[kind] = future.result()
            except BCRAError as exc:
                report[kind] = None
                report["errores"][kind] = str(exc)
    return report


def debt_frame(historical: dict | None, current: dict | None) -> pd.DataFrame:
    """El período actual reemplaza al histórico coincidente para no duplicar deuda.

    Los montos de financiaciones del BCRA se expresan en miles de pesos.
    Se conserva `monto` original y se agrega `monto_ars` para gráficos.
    """
    periods = {}
    for result in (historical, current):
        for period in (result or {}).get("periodos", []):
            periods[period["periodo"]] = period.get("entidades", [])
    rows = []
    for period, entities in sorted(periods.items()):
        for entity in entities:
            rows.append({**entity, "periodo": period, "monto_ars": float(entity.get("monto") or 0) * 1000})
    return pd.DataFrame(rows)


def checks_frame(result: dict | None, entities: dict[int, str] | None = None) -> pd.DataFrame:
    """Una fila por cheque, conservando TODOS sus campos y el contexto."""
    rows = []
    for cause in (result or {}).get("causales", []):
        for entity in cause.get("entidades", []):
            for check in entity.get("detalle", []):
                rows.append({"causal": cause.get("causal"), "entidad": entity.get("entidad"), **check})
    frame = pd.json_normalize(rows)
    if entities is not None and not frame.empty:
        codes = pd.to_numeric(frame["entidad"], errors="coerce")
        names = codes.map(entities)
        fallback = frame["entidad"].map(lambda code: f"Entidad no identificada (código {code})" if pd.notna(code) else "Entidad no informada")
        frame.insert(2, "nombreEntidad", names.fillna(fallback))
    return frame
