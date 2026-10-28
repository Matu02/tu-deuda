"""Comparación opcional sobre una extracción descargada e inmutable."""

from copy import deepcopy
from datetime import date, timedelta
from io import BytesIO

import pandas as pd
import streamlit as st

from comparison import compare_checks, read_previous
from exports import data_downloads


def capture_extraction(frame, snapshot):
    st.session_state["comparison_extraction"] = (frame.copy(deep=True), deepcopy(snapshot))
    st.session_state["comparison_generation"] = st.session_state.get("comparison_generation", 0) + 1


def render_comparison():
    if "comparison_extraction" not in st.session_state:
        return
    generation = st.session_state["comparison_generation"]
    key = f"comparison_{generation}"
    if not st.checkbox("Comparar con una extracción anterior", key=key):
        return
    current, snapshot = st.session_state["comparison_extraction"]
    st.markdown("#### Comparación cheque por cheque")
    st.caption(f"Se compara con la extracción descargada del {snapshot['fecha']}, sin volver a consultar BCRA. Las descargas posteriores inician una comparación nueva.")
    st.caption(f"Alcance actual: fecha de rechazo {snapshot['fecha_rechazo']}" if snapshot.get("fecha_rechazo") else "Alcance actual: toda la historia informada por BCRA.")
    uploaded = st.file_uploader("Detalle anterior de Todos los cheques (Excel/CSV)", type=["xlsx", "csv"], key=f"{key}_upload")
    current_date = date.fromisoformat(snapshot["fecha"])
    previous_date = st.date_input("Fecha de la extracción anterior", value=current_date - timedelta(days=1),
                                  max_value=current_date - timedelta(days=1), key=f"{key}_date")
    if uploaded is None:
        return
    try:
        content = uploaded.getvalue()
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("El archivo supera los 20 MB permitidos.")
        sheet = 0
        if uploaded.name.lower().endswith(".xlsx"):
            with pd.ExcelFile(BytesIO(content), engine="openpyxl") as workbook:
                sheet = st.selectbox("Hoja de la extracción anterior", workbook.sheet_names, key=f"{key}_sheet")
        previous = read_previous(content, uploaded.name, sheet)
        detail, summary = compare_checks(previous, current, snapshot, previous_date)
    except (ValueError, OSError, ImportError, pd.errors.ParserError) as exc:
        st.error(f"No se pudo comparar el archivo: {exc}")
        return
    except Exception:
        st.error("No se pudo leer el archivo anterior. Verificá que sea un Excel .xlsx o CSV válido, sin contraseña.")
        return
    st.caption("Una desaparición indica un posible pago, sin confirmar cancelación ni fecha de pago. Los 15 días desde el rechazo se usan como contexto; no se excluyen cheques más antiguos. Los cheques ya pagados en el archivo anterior no suman nuevos posibles pagos.")
    st.write(f"**{len(detail)} registros comparados · {int(detail['nuevo_posible_pago'].sum())} nuevos posibles pagos**")
    # El selector no altera los archivos exportados.
    options = ["Todos", *sorted(detail["resultado"].unique())]
    filter_key = f"{key}_result"
    if st.session_state.get(filter_key) not in options:
        st.session_state[filter_key] = "Todos"
    selected = st.selectbox("Resultado a mostrar", options, key=filter_key)
    preview = detail if selected == "Todos" else detail.loc[detail["resultado"] == selected]
    st.caption(f"Vista previa de hasta 500 filas de {len(preview)}. La descarga incluye todos los resultados, sin aplicar este filtro.")
    st.dataframe(preview.head(500), hide_index=True, width="stretch")
    data_downloads(detail, f"comparacion_cheques_{previous_date}_{snapshot['fecha']}", "Descargar comparación completa")
    with st.expander("Resumen de nuevos posibles pagos por empresa"):
        st.dataframe(summary.head(500), hide_index=True, width="stretch")
        data_downloads(summary, f"posibles_pagos_{snapshot['fecha']}", "Descargar resumen de posibles pagos")
