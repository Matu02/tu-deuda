# Tu Deuda

App web en Streamlit para consultar la Central de Deudores del BCRA por CUIT.

- Deuda actual y evolución mensual con barras apiladas por entidad financiera.
- Gráfico mensual adicional por situación crediticia (1 a 5), sumando todas las entidades, con descripciones para ambas carteras. Los códigos no clasificados conservan sus montos en una categoría separada.
- Cheques rechazados: gráfico de torta por causal y tabla con todos los campos de cada cheque.
- Nombres de las entidades de cheques desde `/cheques/v1.0/entidades`, con caché de 24 horas. Se conserva el código original; si el catálogo falla o no incluye un código, el detalle de cheques sigue disponible.
- Descargas CSV y respuesta original JSON; manejo independiente de errores por consulta.
- Validación del dígito verificador, consultas paralelas con timeout y reintentos, caché de 15 minutos.

## Ejecutar

Requiere Python 3.12 y acceso HTTPS a `api.bcra.gob.ar`. No necesita claves ni base de datos.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Ingresar el CUIT de la empresa a consultar y pulsar **Consultar BCRA**.

## Pruebas

```bash
.venv/bin/python -m pytest -q
```

Las pruebas usan respuestas simuladas; no dependen de la disponibilidad del BCRA.

## Fuente y unidades

Se consumen los tres endpoints documentados en https://deudores.bcra.apidocs.ar/:

```text
https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/{Identificacion}
https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/Historicas/{Identificacion}
https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas/ChequesRechazados/{Identificacion}
```

Las financiaciones se informan en miles de pesos: la app conserva `monto` y agrega
`monto_ars = monto * 1000` para graficar y sumar en ARS. Los montos de los cheques
ya están en pesos y no se multiplican. Un período presente tanto en la consulta
actual como en la histórica se cuenta una sola vez, priorizando la consulta actual.
Los meses ausentes no se rellenan con ceros. La torta incluye cheques pagados y
no pagados; el monto total rechazado no es necesariamente deuda pendiente.

La tabla conserva todos los campos del detalle del cheque, sumando `causal` y el
código `entidad` de los niveles superiores de la respuesta. La respuesta JSON
completa también está disponible para descarga. Los errores no se guardan en caché.

El catálogo de `/cheques/v1.0/entidades` lista entidades que operan con cheques.
Se agrega `nombreEntidad` sólo con una coincidencia de código; no se supone una
equivalencia para códigos ausentes. Por ejemplo, el código `1` no figura en el
catálogo consultado y se muestra como **Entidad no identificada (código 1)**.

No se incluyen datos empresariales reales en el repositorio. Las consultas sólo
se realizan al enviar el formulario. La aplicación no tiene persistencia propia;
las respuestas se mantienen temporalmente en la memoria del servidor y la sesión.
