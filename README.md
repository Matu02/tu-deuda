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

## Cheques rechazados por archivo

En la pantalla inicial elegir **Cheques por archivo** y adjuntar un Excel `.xlsx`
o un `.csv` (hasta 20 MB). Elegir hoja, columna (A por defecto), primera fila con
datos (2 por defecto) y fecha de rechazo (hoy en Argentina por defecto). Se muestra
una vista previa antes de consultar. Los CUIT se normalizan y validan, se omiten
las filas vacías y se consulta una sola vez cada CUIT único. Las filas inválidas
se informan y se pueden descargar para corregirlas.

Este modo consulta **sólo ChequesRechazados**, no deudas ni historial de deuda.
Como el endpoint no ofrece un filtro por fecha, se descarga su respuesta y se
seleccionan los cheques cuya `fechaRechazo` coincide con la fecha elegida. La
razón social se obtiene de `results.denominacion`. No hay garantía de que el BCRA
ya haya publicado todos los cheques del día; un reporte de hoy puede estar vacío.

El reporte muestra empresas con cheques del día, cantidad y monto total en ARS,
torta por causal, top 10 empresas por monto (con el resto agrupado), y hasta 500
filas de vista previa. Los CSV de detalle y resumen incluyen todos los resultados,
CUIT, razón social, campos originales y nombres de entidades cuando están disponibles.
Los pagos posteriores no excluyen un cheque rechazado en la fecha seleccionada.

### Ritmo, errores y reanudación

- El lote usa un trabajador secuencial en segundo plano y una pausa configurable
  de 1, 2 o 5 segundos, por defecto 2. Para 3.000 CUIT esa pausa suma unos 100 minutos,
  más el tiempo de las respuestas, reintentos y una consulta al catálogo.
- Todas las solicitudes de este proceso, incluidos los reintentos y las consultas
  individuales, comparten un mínimo de 1 segundo entre inicios. No es una cuota
  oficial del BCRA ni se coordina entre distintas réplicas o servidores.
- HTTP 429 respeta `Retry-After` (segundos o fecha HTTP), con espera compartida;
  los HTTP 5xx reciben hasta dos reintentos con espera creciente. Si persiste un
  429, o hay cinco empresas con errores consecutivos, el lote se pausa.
- **Pausar lote** conserva la solicitud en curso y permite continuar. Los errores,
  fechas no interpretables y pendientes se distinguen de empresas sin cheques.
  Los totales parciales se identifican explícitamente.
- **Guardar avance para continuar luego (JSON)** descarga los resultados y los CUIT
  del lote con su fecha. Para recuperarlos, usar **Continuar un lote guardado** y
  luego **Continuar / reintentar pendientes**. No se repiten los CUIT completados;
  los que fallaron o quedaron parciales se vuelven a consultar.
- El avance en vivo reside en la memoria de la sesión. Un reinicio del servidor o
  la pérdida de la sesión puede perder el avance no descargado. No hay una cola
  persistente ni garantía de ejecución en un alojamiento que suspenda el servidor.
  El JSON conserva la fecha original: para volver a consultar los mismos CUIT en
  otro día o refrescar datos ya completados, iniciar un nuevo lote con el archivo.

Los archivos de clientes y avances no se escriben en el repositorio ni se comparten
en una caché global. El JSON descargado contiene información interna. La app no
agrega autenticación: el acceso privado depende del alojamiento que se configure.

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
