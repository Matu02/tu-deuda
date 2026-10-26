"""Cliente público de la Central de Deudores y normalización de sus respuestas."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
ENDPOINTS = {"actual": "", "historicas": "Historicas/", "cheques": "ChequesRechazados/"}


class BCRAError(Exception):
    """Error consultable sin exponer detalles internos de red."""


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


def fetch_endpoint(kind: str, cuit: str) -> dict:
    cuit = normalize_cuit(cuit)
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    try:
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=retry))
            response = session.get(f"{BASE_URL}/{ENDPOINTS[kind]}{cuit}", timeout=(8, 30))
        if response.status_code == 404:
            return {}
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


def checks_frame(result: dict | None) -> pd.DataFrame:
    """Una fila por cheque, conservando TODOS sus campos y el contexto."""
    rows = []
    for cause in (result or {}).get("causales", []):
        for entity in cause.get("entidades", []):
            for check in entity.get("detalle", []):
                rows.append({"causal": cause.get("causal"), "entidad": entity.get("entidad"), **check})
    return pd.json_normalize(rows)
