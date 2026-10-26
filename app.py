"""Tu Deuda · consulta pública de información crediticia del BCRA."""

from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from bcra import checks_frame, debt_frame, fetch_report, normalize_cuit
from charts import checks_chart, debt_chart

st.set_page_config(page_title="Tu Deuda · BCRA", page_icon="🏦", layout="wide")
st.markdown("""<style>
    .block-container {max-width:1280px; padding-top:2.4rem;}
    h1 {letter-spacing:-.04em;}
    [data-testid="stMetric"] {background:white; border:1px solid #e2e8f0;
        border-radius:12px; padding:18px;}
    [data-testid="stMetricValue"] {font-size:1.65rem;}
    .eyebrow {color:#0d9488; font-size:.78rem; font-weight:700; letter-spacing:.15em;}
</style>""", unsafe_allow_html=True)


@st.cache_data(ttl=900, max_entries=64, show_spinner=False)
def load_report(cuit: str):
    report = fetch_report(cuit)
    # Una falla no se cachea: el próximo intento vuelve a consultar al BCRA.
    if report["errores"]:
        raise PartialReport(report)
    return report


class PartialReport(Exception):
    def __init__(self, report):
        self.report = report


def money(value):
    return "$ " + f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def csv_download(frame, name, label):
    st.download_button(label, frame.to_csv(index=False).encode("utf-8-sig"),
                       file_name=name, mime="text/csv")


st.markdown('<div class="eyebrow">INFORMACIÓN CREDITICIA · ARGENTINA</div>', unsafe_allow_html=True)
st.title("Tu Deuda")
st.write("Consultá la deuda financiera y los cheques rechazados de una empresa por CUIT.")

with st.form("consulta"):
    field, action = st.columns([4, 1], vertical_alignment="bottom")
    value = field.text_input("CUIT de la empresa", value="30532847547", placeholder="30-53284754-7")
    submitted = action.form_submit_button("Consultar BCRA", type="primary", width="stretch")

if submitted:
    try:
        cuit = normalize_cuit(value)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()
    with st.spinner("Consultando deudas, historial y cheques en el BCRA…"):
        try:
            st.session_state["report"] = load_report(cuit)
        except PartialReport as exc:
            st.session_state["report"] = exc.report

if "report" not in st.session_state:
    st.info("Ingresá un CUIT y presioná Consultar BCRA para ver el informe.")
    st.caption("Fuente: Banco Central de la República Argentina · Consulta pública, sin claves de acceso.")
    st.stop()

report = st.session_state["report"]
current, historical, checks = (report[k] for k in ("actual", "historicas", "cheques"))
company = next((r.get("denominacion") for r in (current, historical, checks) if r and r.get("denominacion")), "Empresa consultada")
st.subheader(company)
timestamp = datetime.fromisoformat(report["consultado"]).astimezone(ZoneInfo("America/Argentina/Buenos_Aires"))
st.caption(f"CUIT {report['cuit']} · Consultado el {timestamp:%d/%m/%Y a las %H:%M} (Argentina) · Caché de 15 minutos")

labels = {"actual": "Deuda actual", "historicas": "Historial de deuda", "cheques": "Cheques rechazados"}
for kind, error in report["errores"].items():
    st.warning(f"{labels[kind]}: {error} Podés volver a presionar Consultar BCRA para reintentar.")

if all(report[k] is None for k in labels):
    st.error("No se pudo obtener el informe. Intentá nuevamente más tarde.")
    st.stop()
if all(not report[k] for k in labels) and not report["errores"]:
    st.info("El BCRA no devolvió registros para este CUIT en ninguna de las tres consultas.")
    st.stop()

debts = debt_frame(historical, current)
cheques = checks_frame(checks)
current_debts = debt_frame(None, current)
a, b, c = st.columns(3)
if not current_debts.empty:
    last_period = current_debts["periodo"].max()
    latest = current_debts[current_debts["periodo"] == last_period]
    a.metric("Deuda del último período informado", money(latest["monto_ars"].sum()))
    a.caption(f"Período: {last_period[4:]}/{last_period[:4]}")
    b.metric("Entidades con deuda en ese período", latest.loc[latest["monto_ars"] > 0, "entidad"].nunique())
else:
    a.metric("Deuda actual", "No disponible" if current is None else "Sin registros")
    b.metric("Entidades actuales", "—")
c.metric("Cheques rechazados informados", "No disponible" if checks is None else str(len(cheques)))

st.divider()
st.subheader("Evolución de la deuda")
st.caption("Barras apiladas por entidad financiera. Importes en pesos argentinos (ARS).")
if not debts.empty:
    if historical is None:
        st.info("El historial no está disponible; se muestra únicamente el período de la consulta actual.")
    st.plotly_chart(debt_chart(debts), width="stretch")
    st.caption("El BCRA informa las financiaciones en miles de pesos; aquí se multiplican por 1.000. Los meses sin registros no se interpretan como deuda cero.")
    with st.expander("Ver detalle de las deudas por mes y entidad"):
        st.caption("monto: miles de ARS originales del BCRA · monto_ars: pesos argentinos.")
        st.dataframe(debts, hide_index=True, width="stretch")
        csv_download(debts, f"deudas_{report['cuit']}.csv", "Descargar deudas (CSV)")
elif current is None or historical is None:
    st.info("No hay datos de deuda disponibles en las consultas que respondieron.")
else:
    st.info("El BCRA no informa deudas para este CUIT.")

st.divider()
st.subheader("Cheques rechazados")
st.caption("Monto total informado, agrupado por causal de rechazo. Importes en pesos argentinos (ARS).")
if not cheques.empty:
    total = pd.to_numeric(cheques["monto"], errors="raise").sum()
    st.metric("Monto de cheques rechazados", money(total))
    st.caption("Incluye todos los cheques devueltos por la API, incluso los que registran fecha de pago. No representa necesariamente un saldo pendiente.")
    if total > 0:
        st.plotly_chart(checks_chart(cheques), width="stretch")
    else:
        st.info("Los cheques informados no tienen un monto positivo para graficar.")
    st.markdown("#### Detalle de todos los cheques")
    st.caption("Se conservan los nombres y todos los campos de la API. entidad es el código de entidad del BCRA; las celdas vacías corresponden a valores nulos.")
    st.dataframe(cheques, hide_index=True, width="stretch",
                 column_config={"monto": st.column_config.NumberColumn("monto (ARS)", format="%.2f"),
                                "nroCheque": st.column_config.NumberColumn("nroCheque", format="%d"),
                                "entidad": st.column_config.NumberColumn("entidad", format="%d")})
    csv_download(cheques, f"cheques_{report['cuit']}.csv", "Descargar todos los cheques (CSV)")
elif checks is not None:
    st.info("El BCRA no informa cheques rechazados para este CUIT.")
else:
    st.info("No se pudo obtener la información de cheques rechazados.")

with st.expander("Respuestas originales del BCRA"):
    st.json({k: report[k] for k in labels}, expanded=False)
    st.download_button("Descargar informe (JSON)", json.dumps(report, ensure_ascii=False, indent=2),
                       file_name=f"bcra_{report['cuit']}.json", mime="application/json")
st.caption("Fuente: Central de Deudores del BCRA · Los períodos y registros disponibles dependen de la publicación del organismo. [Documentación de la API](https://deudores.bcra.apidocs.ar/)")
