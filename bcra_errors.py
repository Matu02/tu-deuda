"""Excepciones compartidas entre el cliente BCRA, los lotes y la interfaz."""


class BCRAError(Exception):
    """Error consultable sin exponer detalles internos de red."""


class BCRARateLimit(BCRAError):
    """El servidor sigue limitando las solicitudes luego de los reintentos."""


class BCRACooldown(BCRAError):
    """El servicio pidió una espera larga; devolver el control a la interfaz."""


class BCRACancelled(BCRAError):
    """Consulta pausada por el usuario."""
