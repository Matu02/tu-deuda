"""Pantalla de análisis agregado de cheques y deudas por archivo."""

from datetime import datetime
from io import BytesIO
import json
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from bcra_errors import BCRAError, BCRAUnavailable
from bulk import BatchJob, debt_report_frames, extract_cuits, read_table, report_frames, restore_job
from charts import checks_chart, companies_chart, debt_chart, situation_chart
from formatting import money
from exports import data_downloads
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
    st.subheader("Análisis agregado por archivo")
    st.write("Adjuntá la lista de clientes y elegí qué información consultar en la Central de Deudores.")
    selected = st.selectbox("Análisis a solicitar", ["Cheques rechazados", "Deudas", "Ambos"])
    analysis = {"Cheques rechazados": "cheques", "Deudas": "deudas", "Ambos": "ambos"}[selected]
    today = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    target = today
    scope = st.radio("Cheques a incluir", ["Toda la historia informada por el BCRA", "Una fecha de rechazo específica"]) if analysis != "deudas" else None
    rejection_date = st.date_input("Fecha de rechazo", value=today, max_value=today).isoformat() if scope == "Una fecha de rechazo específica" else None
    uploaded = st.file_uploader("Insertar Excel/CSV con CUITs", type=["xlsx", "csv"], key="bulk_upload", help="Excel .xlsx o CSV de hasta 20 MB. Podés elegir la columna y la primera fila con CUIT.")
    st.caption("Ritmo preventivo de la app: hasta 3 solicitudes por segundo en total, compartidas por ambos endpoints, reintentos y sesiones de este servidor. Hasta 3 consultas en curso por lote. En Ambos se consultan cheques y deudas de forma concurrente: 2 solicitudes por CUIT. Las esperas indicadas por el BCRA se respetan automáticamente.")
    st.caption("Deudas consulta el último período informado por el endpoint de deuda actual. Los montos originales están en miles de pesos y se convierten a ARS. No se suman saldos de meses distintos.")
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
            st.caption(f"Tiempo orientativo a 3 solicitudes por segundo: {len(cuits) * (2 if analysis == "ambos" else 1) / REQUESTS_PER_SECOND / 60:.0f} minutos. Se cuentan 2 solicitudes por CUIT en Ambos; puede demorar más según las respuestas, reintentos y límites del BCRA.")
            if not invalid.empty:
                with st.expander("Filas que no se consultarán"):
                    st.dataframe(invalid, hide_index=True, width="stretch")
                    data_downloads(invalid, "cuits_invalidos", "Descargar filas inválidas")
            if st.button("Iniciar nuevo lote", type="primary", disabled=not cuits or _active()):
                if analysis != "deudas":
                    with st.spinner("Preparando catálogo de entidades…"):
                        if not _catalog(load_entities):
                            return
                job = BatchJob(cuits, target, rejection_date=rejection_date, analysis=analysis)
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
                if job.analysis != "deudas" and job.can_resume() and not _catalog(load_entities):
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
    states = [snapshot["resultados"].get(c, {}).get("estados_consulta", {}) for c in snapshot["cuits"]]
    pending = sum(any(s.get(k, "pendiente") == "pendiente" for k in job.kinds) for s in states)
    attempted = len(snapshot["cuits"]) - pending
    requests_done = sum(s.get(k, "pendiente") != "pendiente" for s in states for k in job.kinds)
    total = len(snapshot["cuits"])
    ok = int((summary["estado"] == "ok").sum()) if not summary.empty else 0
    problems = int(summary["estado"].isin(["error", "parcial"]).sum()) if not summary.empty else 0
    st.divider()
    st.subheader(f"Reporte agregado · {snapshot['fecha']}")
    st.info(f"Análisis del lote: {dict(cheques="Cheques rechazados", deudas="Deudas", ambos="Ambos")[job.analysis]}")
    if job.analysis != "deudas":
        st.info(f"Filtro del lote: fecha de rechazo {job.rejection_date}" if job.rejection_date else "Filtro del lote: toda la historia informada por el BCRA")
    st.caption("Cambiar los controles superiores no modifica este lote. Para cambiar el análisis o filtro, iniciá un nuevo lote.")
    st.progress(requests_done / (total * len(job.kinds)) if total else 0, text=f"{attempted} de {total} CUIT procesados · {ok} completos · {problems} con errores o datos parciales · {pending} pendientes")
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
        st.warning("Reporte parcial: los totales incluyen sólo las respuestas obtenidas. Los análisis pendientes o con error no se consideran empresas sin deuda ni sin cheques.")
    if job.analysis != "deudas":
        render_checks(snapshot, frame, summary, pending, problems)
    if job.analysis != "cheques":
        render_debts(snapshot)
    with st.expander("Estado de cada CUIT, incluidos errores y pendientes"):
        st.dataframe(summary.head(500), hide_index=True, width="stretch")
        data_downloads(summary, f"empresas_{snapshot['fecha']}", "Descargar resumen de empresas")
    st.download_button("Guardar avance para continuar luego (JSON)", json.dumps(snapshot, ensure_ascii=False),
                       f"avance_{snapshot['fecha']}.json", "application/json")


def render_checks(snapshot, frame, summary, pending, problems):
    if not summary.empty and summary["fechas_invalidas"].sum():
        st.warning("Hay fechas de rechazo no informadas o no interpretables. En historia completa se conservan; con fecha específica no pueden asignarse al día y el CUIT queda parcial.")
    if st.session_state.get("bulk_catalog_error"):
        st.caption("Catálogo no disponible: se conservan los códigos de entidad originales.")
    count = frame["CUIT"].nunique() if not frame.empty else 0
    amount = frame["monto"].sum() if not frame.empty else 0
    a, b, c = st.columns(3)
    a.metric("Empresas con cheques rechazados", count)
    b.metric("Monto total rechazado (ARS)", money(amount))
    c.metric("Cheques rechazados", len(frame))
    st.caption("Incluye los cheques correspondientes al filtro indicado, incluso los pagados posteriormente. El total rechazado no representa necesariamente saldo pendiente.")
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
        data_downloads(frame, f"cheques_lote_{snapshot['fecha']}", "Descargar todos los cheques")
    elif not snapshot["running"] and not pending and not problems:
        st.info("El BCRA no informó cheques rechazados para los CUIT consultados.")


def render_debts(snapshot):
    frame, summary = debt_report_frames(snapshot)
    st.markdown("### Deuda agregada")
    completed = int((summary["estado"] == "ok").sum()) if not summary.empty else 0
    st.caption(f"{completed} de {len(snapshot['cuits'])} CUIT con consulta de deuda completa. Los errores y pendientes no se contabilizan como deuda cero.")
    if not frame.empty:
        periods = sorted(frame["periodo"].unique(), reverse=True)
        period = st.selectbox("Período de deuda a analizar", periods,
                              format_func=lambda p: f"{p[:4]}-{p[4:]}", key="bulk_debt_period")
        selected = frame.loc[frame["periodo"] == period].copy()
        selected["entidad"] = selected["entidad"].fillna("Entidad no informada")
        amount = selected["monto_ars"].sum()
        a, b, c = st.columns(3)
        a.metric("Empresas con deuda en el período", selected.loc[selected["monto_ars"] > 0, "CUIT"].nunique())
        b.metric("Deuda total del período (ARS)", money(amount))
        c.metric("Entidades financieras", selected["entidad"].nunique())
        st.caption("Cada total corresponde únicamente al período seleccionado. Los CUIT cuyo último período informado sea distinto no aportan a ese total; consultá el resumen por empresa. El detalle conserva monto original en miles de pesos y monto_ars en pesos.")
        if amount > 0:
            left, right = st.columns(2)
            with left:
                st.markdown("#### Deuda por entidad")
                st.plotly_chart(debt_chart(selected), width="stretch")
            with right:
                st.markdown("#### Deuda por situación crediticia")
                st.plotly_chart(situation_chart(selected), width="stretch")
            st.markdown("#### Top 10 empresas por deuda")
            st.plotly_chart(companies_chart(selected.assign(monto=selected["monto_ars"])), width="stretch")
        st.markdown("#### Detalle de deudas del período")
        st.caption(f"Vista previa de hasta 500 filas de {len(selected)}. La descarga de deudas incluye todas las filas y períodos recibidos.")
        st.dataframe(selected.head(500).style.format({"monto_ars": money}, na_rep="—"), hide_index=True, width="stretch")
        data_downloads(frame, f"deudas_lote_{snapshot['fecha']}", "Descargar todas las deudas")
    elif completed == len(snapshot["cuits"]) and not snapshot["running"]:
        st.info("El BCRA no informó deudas para los CUIT consultados.")
    else:
        st.info("Todavía no hay registros de deuda disponibles. Revisá los estados de consulta.")
    with st.expander("Resumen de deuda y estado por empresa"):
        st.caption("Deuda y período más reciente de cada CUIT. Los saldos de períodos distintos no se suman entre sí.")
        st.dataframe(summary.head(500), hide_index=True, width="stretch")
        data_downloads(summary, f"resumen_deudas_{snapshot['fecha']}", "Descargar resumen de deudas")
