"""Excepciones compartidas entre el cliente BCRA, los lotes y la interfaz."""


class BCRAError(Exception):
    """Error consultable sin exponer detalles internos de red."""


class BCRARateLimit(BCRAError):
    """El servidor sigue limitando las solicitudes luego de los reintentos."""


class BCRACooldown(BCRAError):
    """El servicio pidió una espera larga; devolver el control a la interfaz."""


class BCRACancelled(BCRAError):
    """Consulta pausada por el usuario."""


class BCRAUnavailable(BCRACooldown):
    """HTTP 503: detener el flujo, con plazo opcional de reanudación."""

    def __init__(self, retry_at=None):
        from datetime import datetime, timezone
        import math
        self.retry_at = retry_at
        if retry_at:
            seconds = max(0, math.ceil((datetime.fromisoformat(retry_at) - datetime.now(timezone.utc)).total_seconds()))
            message = f"Caída la API BCRA. Reintentá en {seconds} segundos."
        else:
            message = "Caída la API BCRA. Reintentá más tarde."
        super().__init__(message)
