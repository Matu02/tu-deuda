# Tu Deuda

App web en Streamlit para consultar la Central de Deudores del BCRA por CUIT.

- Deuda actual y evolución mensual con barras apiladas por entidad financiera.
- Gráfico mensual adicional por situación crediticia (1 a 5), sumando todas las entidades, con descripciones para ambas carteras. Los códigos no clasificados conservan sus montos en una categoría separada.
- Cheques rechazados: gráfico de torta por causal y tabla con todos los campos de cada cheque.
- Nombres de las entidades de cheques desde `/cheques/v1.0/entidades`, con caché de 24 horas. Se conserva el código original; si el catálogo falla o no incluye un código, el detalle de cheques sigue disponible.
- Descargas Excel (.xlsx), CSV y respuesta original JSON; manejo independiente de errores por consulta.
- Validación del dígito verificador, consultas paralelas con timeout y reintentos, caché de 15 minutos.

## Ejecutar

Requiere Python 3.12 y acceso HTTPS a `api.bcra.gob.ar`. No necesita claves ni base de datos.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Ingresar el CUIT de la empresa a consultar y pulsar **Consultar BCRA**.

## Análisis agregado por archivo

Debajo del buscador individual, usar **Insertar Excel/CSV con CUITs** para adjuntar un Excel `.xlsx`
o un `.csv` (hasta 20 MB). Elegir hoja, columna (A por defecto), primera fila con
datos (2 por defecto). Se muestra
una vista previa antes de consultar. Los CUIT se normalizan y validan, se omiten
las filas vacías y se consulta una sola vez cada endpoint seleccionado por CUIT único. Las filas inválidas
se informan y se pueden descargar para corregirlas.

El selector **Análisis a solicitar** permite elegir **Cheques rechazados**, **Deudas** o
**Ambos**. Deudas usa `Deudas/{Identificacion}` (deuda actual), sin solicitar el
historial. Ambos agenda los dos endpoints de forma concurrente en el mismo grupo
de tres trabajadores; ambos comparten el límite de ritmo del cliente.

El análisis de deuda muestra total en ARS, empresas con deuda, entidades,
distribución por entidad y situación crediticia, y top 10 empresas. Se elige un
período para cada agregado: no se suman saldos de meses distintos. Los montos de
financiaciones se convierten de miles de pesos a pesos. El detalle y resumen por
CUIT se descargan en Excel o CSV; los errores y pendientes no se cuentan como cero.
Por defecto se registran **todos** los cheques que devuelve la API. También se
puede elegir una fecha de rechazo específica en el selector de alcance. La razón social se obtiene de `results.denominacion`. La fecha
mostrada en el título identifica el inicio de la consulta, no restringe los resultados.

El reporte muestra empresas con cheques, cantidad y monto total en ARS, torta por
causal, top 10 empresas por monto (con el resto agrupado), y hasta 500 filas de
vista previa. Las descargas Excel y CSV incluyen todos los resultados, CUIT, razón social y todos
los campos de cada cheque. Se incluyen cheques pagados y con fecha de rechazo
vacía o no interpretable; el monto rechazado no es necesariamente saldo pendiente.

Los nuevos archivos de avance usan versión 4 y guardan el tipo de análisis, el filtro y el estado de cada endpoint. Se aceptan avances v1, v2 y v3 como consultas de cheques. Al cargar uno de versión 1, que
filtraba por fecha, los CUIT completados se marcan como parciales para volver a
consultarlos. Los resultados descartados no pueden recuperarse del JSON viejo:
se necesita una nueva consulta. Los avances v2 completos no repiten esos CUIT.

### Verificación del límite de la API (27 de octubre de 2026)

Se revisaron el [manual oficial de Central de Deudores](https://www.bcra.gob.ar/archivos/Catalogo/Content/files/pdf/central-deudores-v1.pdf),
la [especificación OpenAPI oficial](https://www.bcra.gob.ar/archivos/Catalogo/Content/files/json/central-deudores-v1.json)
y el [catálogo de APIs del BCRA](https://www.bcra.gob.ar/apis-banco-central/).
Estas fuentes no indican una cuota numérica ni su alcance por endpoint, API o IP.
No se puede afirmar que exista una cuota oficial de 3 solicitudes por segundo o
una solicitud cada 3 segundos. Los **3 segundos** del manejo de HTTP 429 son una
espera mínima preventiva del cliente, salvo que el servidor pida una mayor.

Se mantiene un ritmo preventivo de **3 solicitudes por segundo en total** para
los endpoints y sesiones del proceso. Esto permite consultas en curso simultáneas,
espaciando sus inicios al menos 1/3 de segundo. No se hicieron pruebas de carga
contra el BCRA para inferir una cuota no publicada. `Retry-After`, HTTP 429 y HTTP
503 siguen controlando esperas y pausas globales.

### Ritmo, errores y reanudación

- El lote usa hasta tres trabajadores en segundo plano, con un límite compartido
  de **3 solicitudes por segundo**. Para 3.000 CUIT, el tiempo teórico es de unos
  17 minutos con un análisis o 34 minutos con Ambos, sujeto al tiempo de respuesta, reintentos y una consulta al catálogo.
- Todas las solicitudes de este proceso, incluidos los reintentos y las consultas
  individuales, comparten un mínimo de 1/3 de segundo entre inicios. No es una cuota
  oficial del BCRA ni se coordina entre distintas réplicas o servidores.
- HTTP 429 sin `Retry-After` válido espera **3 segundos**. Con cabecera válida,
  respeta esa espera con mínimo de 3 segundos. Hasta tres intentos totales por solicitud,
  con espera compartida entre trabajadores. Si persiste el límite, el lote se pausa.
- Todo HTTP 503 detiene el lote inmediatamente, incluso al consultar el catálogo,
  sin reintentos automáticos. Muestra «Caída la API BCRA»: con el plazo indicado
  por `Retry-After`, o «Reintentá más tarde» si no hay un plazo válido. Las consultas
  ya enviadas pueden finalizar y sus resultados se conservan.
- El plazo de un 503 se conserva como `reintentar_desde` en los nuevos archivos de
  avance; se impide continuar antes de que venza. Los JSON anteriores siguen siendo
  válidos. Sin plazo indicado, la reanudación es manual, sin espera inventada.
- Los errores HTTP 500, 502 y 504 mantienen sus reintentos; cinco consultas con
  errores consecutivos pausan el lote. Los errores nunca se cuentan como cero cheques.
- **Pausar lote** conserva los resultados de las consultas en curso y permite continuar. Los errores,
  fechas no interpretables y pendientes se distinguen de empresas sin cheques.
  Los totales parciales se identifican explícitamente.
- **Guardar avance para continuar luego (JSON)** descarga los resultados y los CUIT
  del lote con su fecha. Para recuperarlos, usar **Continuar un lote guardado** y
  luego **Continuar / reintentar pendientes**. No se repiten los endpoints completados;
  sólo se reintentan los pendientes, fallidos o parciales, conservando el otro análisis.
- El avance en vivo reside en la memoria de la sesión. Un reinicio del servidor o
  la pérdida de la sesión puede perder el avance no descargado. No hay una cola
  persistente ni garantía de ejecución en un alojamiento que suspenda el servidor.
  El JSON conserva la fecha original de consulta. Para refrescar resultados ya
  completados, iniciar un nuevo lote con el archivo.

Los archivos de clientes y avances no se escriben en el repositorio ni se comparten
en una caché global. El JSON descargado contiene información interna. La app no
agrega autenticación: el acceso privado depende del alojamiento que se configure.

### Comparar con una extracción anterior

Después de descargar **Todos los cheques** en Excel o CSV, se habilita
**Comparar con una extracción anterior**. Adjuntá el detalle Excel/CSV de otra
extracción de Tu Deuda (hasta 20 MB) e indicá su fecha. Se propone el día anterior;
también se pueden comparar extracciones más antiguas. El resumen de empresas y
el avance JSON no sirven como detalle anterior.

La comparación se hace **cheque por cheque**, usando CUIT, entidad, número de
cheque y fecha de rechazo, con formatos normalizados. No realiza nuevas consultas
a BCRA: conserva una copia de los datos y estados de la descarga actual. Una nueva
descarga de Todos los cheques reinicia la comparación.

La tabla distingue **Sigue informado**, **Posible pago: dejó de figurar**,
**Nuevo en la extracción actual** y **No comparable**. La vista previa muestra
hasta 500 filas y permite filtrar por resultado. **Descargar comparación completa
(Excel)** incluye todas las filas, sin aplicar ese filtro, con los montos anteriores
y actuales, fechas de extracción y los campos originales de ambos archivos.
También se puede descargar en CSV y obtener un resumen de nuevos posibles pagos
por empresa.

Una desaparición no confirma el pago ni su fecha. Los registros que ya indicaban
pago en el archivo anterior se identifican y no suman nuevos posibles pagos. La
antigüedad desde el rechazo se informa como contexto: no se excluyen cheques por
superar los 15 días. Las empresas no consultadas, las consultas de cheques
pendientes, parciales o fallidas, los cheques fuera del filtro actual y las
identidades incompletas o duplicadas ambiguas quedan como No comparable.
Las extracciones vacías también se pueden descargar y comparar.

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


## Selección de alcance del lote

El selector **Cheques a incluir** permite elegir **Toda la historia informada por
el BCRA** (predeterminado) o **Una fecha de rechazo específica**. En este último
caso se muestra un calendario y se filtra por `fechaRechazo`. Historia completa
significa todos los registros que devuelve la API, sin garantizar información
que el BCRA ya no publique. Ambos modos incluyen cheques pagados posteriormente.
El reporte identifica el filtro aplicado. Cambiar el selector no altera un lote
existente: hay que iniciar uno nuevo. Los avances v3 guardan `fecha_rechazo`
(null para historia completa); los v2 se recuperan como historia completa y los
v1 conservan la migración que requiere volver a consultar los CUIT filtrados.
