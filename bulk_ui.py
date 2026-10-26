"""Pantalla del reporte diario de cheques por archivo."""

from datetime import datetime
from io import BytesIO
import json
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from bcra_errors import BCRAError, BCRAUnavailable
from bulk import BatchJob, extract_cuits, read_table, report_frames, restore_job
from charts import checks_chart, companies_chart
from formatting import money
from rate_policy import REQUESTS_PER_SECOND


def _catalog(load_entities):
    try:
        st.session_state["bulk_catalog"] = load_entities()
        st.session_state["bulk_catalog_error"] = False
        return True
    except BCRAUnavailable as exc:
        st.error(str(exc))
        job = st.session_state.get("bulk_job")
        if job is not None:
            job.retry_at = exc.retry_at
            job.message = str(exc)
        return False
    except BCRAError:
        st.session_state["bulk_catalog"] = {}
        st.session_state["bulk_catalog_error"] = True
        return True


def _active():
    job = st.session_state.get("bulk_job")
    return job is not None and job.snapshot()["running"]


def render_bulk(load_entities):
    st.subheader("Cheques rechazados por archivo")
    st.write("Adjuntá la lista de clientes. Se registran todos los cheques rechazados que informa el BCRA para cada CUIT, sin filtrar por fecha de rechazo.")
    today = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    target = today
    uploaded = st.file_uploader("Insertar Excel/CSV con CUITs", type=["xlsx", "csv"], key="bulk_upload", help="Excel .xlsx o CSV de hasta 20 MB. Podés elegir la columna y la primera fila con CUIT.")
    st.caption("Hasta 3 solicitudes por segundo y 3 consultas simultáneas por lote. Una consulta de cheques por CUIT único, más el catálogo de entidades. El ritmo se comparte entre sesiones de este servidor e incluye los reintentos; no se coordina entre servidores distintos. Las esperas indicadas por el BCRA se respetan automáticamente.")
    if uploaded is not None:
        try:
            content = uploaded.getvalue()
            if len(content) > 20 * 1024 * 1024:
                raise ValueError("El archivo supera los 20 MB permitidos.")
            sheet = 0
            if uploaded.name.lower().endswith(".xlsx"):
                with pd.ExcelFile(BytesIO(content), engine="openpyxl") as workbook:
                    sheet = st.selectbox("Hoja del Excel", workbook.sheet_names)
            table = read_table(content, uploaded.name, sheet)
            if table.empty:
                raise ValueError("El archivo está vacío.")
            st.caption("Vista del archivo sin interpretar encabezados. La numeración de filas empieza en 1.")
            preview = table.head(10).copy()
            preview.index = range(1, len(preview) + 1)
            st.dataframe(preview, width="stretch")
            a, b = st.columns(2)
            row = a.number_input("Primera fila con CUIT", min_value=1, max_value=len(table), value=min(2, len(table)), step=1)
            column = b.selectbox("Columna de CUIT (A = primera columna)", options=list(range(len(table.columns))), format_func=lambda i: f"Columna {i + 1}" + (" (A)" if i == 0 else ""))
            cuits, invalid, duplicates, blanks = extract_cuits(table, int(row), column)
            st.write(f"**{len(cuits):,} CUIT válidos únicos** · {duplicates:,} duplicados · {len(invalid):,} filas inválidas · {blanks:,} vacías".replace(",", "."))
            st.caption(f"Tiempo orientativo a 3 solicitudes por segundo: {len(cuits) / REQUESTS_PER_SECOND / 60:.0f} minutos. Para 3.000 CUIT, unos 17 minutos; puede demorar más según las respuestas, reintentos y límites del BCRA.")
            if not invalid.empty:
                with st.expander("Filas que no se consultarán"):
                    st.dataframe(invalid, hide_index=True, width="stretch")
                    st.download_button("Descargar filas inválidas", invalid.to_csv(index=False).encode("utf-8-sig"), "cuits_invalidos.csv", "text/csv")
            if st.button("Iniciar nuevo lote", type="primary", disabled=not cuits or _active()):
                with st.spinner("Preparando catálogo de entidades…"):
                    if not _catalog(load_entities):
                        return
                job = BatchJob(cuits, target)
                st.session_state["bulk_job"] = job
                job.start()
        except (ValueError, OSError, ImportError, pd.errors.ParserError) as exc:
            st.error(f"No se pudo importar el archivo: {exc}")
        except Exception:
            st.error("No se pudo leer el archivo. Verificá que sea un Excel .xlsx o CSV válido, sin contraseña.")

    with st.expander("Continuar un lote guardado"):
        checkpoint = st.file_uploader("Archivo de avance de Tu Deuda (JSON)", type=["json"], key="bulk_checkpoint")
        if st.button("Cargar avance", disabled=checkpoint is None or _active()):
            try:
                job = restore_job(checkpoint.getvalue())
                st.session_state["bulk_job"] = job
                if job.can_resume() and not _catalog(load_entities):
                    return
                st.success(f"Avance cargado para el {job.target_date}. Presioná Continuar / reintentar pendientes.")
            except ValueError as exc:
                st.error(str(exc))
    st.caption("El trabajo y sus resultados se mantienen en la memoria de esta sesión del servidor. Descargá el avance para recuperarlo si se reinicia el servidor o se pierde la sesión. El archivo de avance contiene CUIT y resultados: guardalo como información interna.")
    if "bulk_job" in st.session_state:
        render_progress()


@st.fragment(run_every="5s")
def render_progress():
    job = st.session_state["bulk_job"]
    snapshot = job.snapshot()
    frame, summary = report_frames(snapshot, st.session_state.get("bulk_catalog", {}))
    attempted = len(snapshot["resultados"])
    total = len(snapshot["cuits"])
    ok = int((summary["estado"] == "ok").sum()) if not summary.empty else 0
    problems = int(summary["estado"].isin(["error", "parcial"]).sum()) if not summary.empty else 0
    pending = total - attempted
    st.divider()
    st.subheader(f"Reporte de cheques · {snapshot['fecha']}")
    st.caption("La fecha del reporte corresponde al inicio de la consulta, no a la fecha de rechazo. Se incluyen todos los cheques informados.")
    st.progress(attempted / total if total else 0, text=f"{attempted} de {total} CUIT procesados · {ok} completos · {problems} con errores o datos parciales · {pending} pendientes")
    st.caption(snapshot["mensaje"])
    if not job.can_resume():
        st.error(str(BCRAUnavailable(job.retry_at)))
    controls = st.columns(2)
    if controls[0].button("Pausar lote", disabled=not snapshot["running"]):
        job.pause()
        st.info("Se pausará al terminar las consultas en curso; no se pierden las consultas completadas.")
    if controls[1].button("Continuar / reintentar pendientes", disabled=snapshot["running"] or ok == total or not job.can_resume()):
        job.start()
        st.rerun()
    if snapshot["running"] or pending or problems:
        st.warning("Reporte parcial: los totales corresponden sólo a los cheques obtenidos hasta ahora. Los CUIT pendientes o con error no se consideran empresas sin cheques.")
    if not summary.empty and summary["fechas_invalidas"].sum():
        st.warning("Hay cheques con fechas de rechazo no informadas o no interpretables. Se conservan en el detalle y se incluyen en los totales.")
    if st.session_state.get("bulk_catalog_error"):
        st.caption("Catálogo no disponible: se conservan los códigos de entidad originales.")
    count = frame["CUIT"].nunique() if not frame.empty else 0
    amount = frame["monto"].sum() if not frame.empty else 0
    a, b, c = st.columns(3)
    a.metric("Empresas con cheques rechazados", count)
    b.metric("Monto total rechazado (ARS)", money(amount))
    c.metric("Cheques rechazados", len(frame))
    st.caption("Incluye todos los cheques devueltos por el BCRA, incluso los pagados posteriormente. El total rechazado no representa necesariamente saldo pendiente.")
    if not frame.empty:
        if amount > 0:
            left, right = st.columns(2)
            with left:
                st.markdown("#### Monto rechazado por causal")
                st.plotly_chart(checks_chart(frame), width="stretch")
            with right:
                st.markdown("#### Top 10 empresas por monto rechazado")
                st.plotly_chart(companies_chart(frame), width="stretch")
                st.caption("Las demás empresas se agrupan como Resto de empresas para representar el total del lote.")
        st.markdown("#### Detalle de cheques")
        st.caption(f"Vista previa de hasta 500 filas de {len(frame):,}. La descarga incluye todas las filas y todos los campos.".replace(",", "."))
        st.dataframe(frame.head(500).style.format({"monto": money}, na_rep="—"), hide_index=True, width="stretch",
                     column_config={"monto": "monto (ARS)", "CUIT": st.column_config.TextColumn("CUIT"),
                                    "razon_social": "Razón social", "nroCheque": st.column_config.NumberColumn(format="%d")})
        st.download_button("Descargar todos los cheques (CSV)", frame.to_csv(index=False).encode("utf-8-sig"),
                           f"cheques_lote_{snapshot['fecha']}.csv", "text/csv")
    elif not snapshot["running"] and not pending and not problems:
        st.info("El BCRA no informó cheques rechazados para los CUIT consultados.")
    with st.expander("Estado de cada CUIT, incluidos errores y pendientes"):
        st.dataframe(summary.head(500), hide_index=True, width="stretch")
        st.download_button("Descargar resumen de empresas (CSV)", summary.to_csv(index=False).encode("utf-8-sig"),
                           f"empresas_{snapshot['fecha']}.csv", "text/csv")
    st.download_button("Guardar avance para continuar luego (JSON)", json.dumps(snapshot, ensure_ascii=False),
                       f"avance_cheques_{snapshot['fecha']}.json", "application/json")
