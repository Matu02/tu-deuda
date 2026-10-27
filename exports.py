"""Descargas de tablas en CSV y Excel."""

from io import BytesIO

import pandas as pd
import streamlit as st


def excel_bytes(frame):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Datos")
        sheet = writer.sheets["Datos"]
        # Los textos del BCRA o del archivo importado son datos, incluso si
        # empiezan con '='. Conservar también los CUIT como texto.
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    return output.getvalue()


def data_downloads(frame, name, label):
    csv, excel = st.columns(2)
    csv.download_button(
        f"{label} (CSV)", frame.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{name}.csv", mime="text/csv",
    )
    excel.download_button(
        f"{label} (Excel)", excel_bytes(frame), file_name=f"{name}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
