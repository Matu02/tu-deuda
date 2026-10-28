from copy import deepcopy
from unittest.mock import Mock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from bulk import BatchJob, report_frames
from comparison import POSSIBLE_PAYMENT
from comparison_ui import capture_extraction


CUIT = "30000000007"


def empty_job():
    return BatchJob([CUIT], "2026-01-20", records={CUIT: {
        "estado": "ok", "cheques": [], "razon_social": "Empresa",
        "estados_consulta": {"cheques": "ok"}, "fechas_invalidas": 0,
    }})


def test_capture_freezes_downloaded_data_and_resets_generation():
    state = {}
    frame = pd.DataFrame({"monto": [100]})
    snapshot = empty_job().snapshot()
    with patch("comparison_ui.st.session_state", state):
        capture_extraction(frame, snapshot)
        frame.loc[0, "monto"] = 0
        snapshot["resultados"][CUIT]["estado"] = "error"
        frozen_frame, frozen_snapshot = state["comparison_extraction"]
        assert frozen_frame.iloc[0]["monto"] == 100
        assert frozen_snapshot["resultados"][CUIT]["estado"] == "ok"
        capture_extraction(frame, snapshot)
        assert state["comparison_generation"] == 2


def test_preview_filter_and_full_export_without_new_requests():
    job = empty_job()
    frame, _ = report_frames(job.snapshot())
    previous = pd.DataFrame([{
        "CUIT": CUIT, "razon_social": "Empresa", "entidad": 11,
        "nroCheque": i, "fechaRechazo": "2026-01-01", "monto": 100,
    } for i in range(501)])
    upload = Mock()
    upload.name = "cheques.csv"
    upload.getvalue.return_value = previous.to_csv(index=False).encode()

    def uploader(label, **kwargs):
        return upload if kwargs.get("key", "").startswith("comparison_") else None

    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.run()
    assert not app.exception
    assert not any(c.label == "Comparar con una extracción anterior" for c in app.checkbox)
    assert any(d.proto.label == "Descargar todos los cheques (Excel)" for d in app.get("download_button"))
    app.session_state["comparison_extraction"] = (frame, deepcopy(job.snapshot()))
    app.session_state["comparison_generation"] = 1
    app.run()
    with patch("streamlit.file_uploader", side_effect=uploader), patch("bulk.fetch_endpoint") as fetch, patch("comparison_ui.data_downloads") as downloads:
        next(c for c in app.checkbox if c.label == "Comparar con una extracción anterior").check().run()
        assert not app.exception
        preview = next(d.value for d in app.dataframe if "resultado" in d.value)
        assert len(preview) == 500
        assert set(preview["resultado"]) == {POSSIBLE_PAYMENT}
        assert len(downloads.call_args_list[0].args[0]) == 501
        next(s for s in app.selectbox if s.label == "Resultado a mostrar").select(POSSIBLE_PAYMENT).run()
        assert not app.exception
        assert len(downloads.call_args_list[-2].args[0]) == 501
        fetch.assert_not_called()


def test_check_downloads_are_wired_to_capture_callback():
    from exports import data_downloads

    with patch("exports.st.columns") as columns:
        columns.return_value = (Mock(), Mock())
        frame = pd.DataFrame({"monto": [100]})
        snapshot = empty_job().snapshot()
        data_downloads(frame, "cheques", "Cheques", on_click=capture_extraction, args=(frame, snapshot))
        for column in columns.return_value:
            kwargs = column.download_button.call_args.kwargs
            assert kwargs["on_click"] is capture_extraction
            assert kwargs["args"][1] is snapshot
