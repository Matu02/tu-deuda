from datetime import date
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from bulk import BatchJob, daily_result


def sample():
    return {"denominacion": "Empresa de prueba", "causales": [{"causal": "SIN FONDOS", "entidades": [
        {"entidad": 11, "detalle": [
            {"nroCheque": 1, "monto": 1234567.89, "fechaRechazo": "2026-01-12"},
            {"nroCheque": 2, "monto": 9999999, "fechaRechazo": "2026-01-11"},
        ]}]}]}


def test_upload_to_daily_report_without_any_debt_requests():
    uploaded = Mock(name="upload")
    uploaded.name = "clientes.csv"
    uploaded.getvalue.return_value = b"CUIT\n30000000007\n30-00000000-7\ninvalid\n"

    def upload(label, **kwargs):
        return None if "avance" in label else uploaded

    def start(job):
        job.running = True
        job.stop_event.wait = Mock(return_value=False)
        job._run()

    with patch("streamlit.file_uploader", side_effect=upload), patch("bcra.fetch_entities", return_value={11: "Banco de prueba"}), patch("bulk.fetch_endpoint", return_value=sample()) as fetch, patch.object(BatchJob, "start", start):
        app = AppTest.from_file("app.py").run()
        app.radio[0].set_value("Cheques por archivo").run()
        assert not app.exception
        app.date_input[0].set_value(date(2026, 1, 12)).run()
        next(b for b in app.button if b.label == "Iniciar nuevo lote").click().run()
    assert not app.exception
    assert fetch.call_count == 1 and fetch.call_args.args[0] == "cheques"
    assert [m.value for m in app.metric] == ["1", "$ 1.234.567,89", "1"]
    assert len(app.get("plotly_chart")) == 2
    detail = next(d.value for d in app.dataframe if "razon_social" in d.value and "nroCheque" in d.value)
    assert len(detail) == 1
    assert detail.loc[0, "CUIT"] == "30000000007"
    assert detail.loc[0, "razon_social"] == "Empresa de prueba"
    assert detail.loc[0, "nombreEntidad"] == "Banco de prueba"


def test_failed_or_pending_cuits_never_claim_a_complete_empty_report():
    job = BatchJob(["30000000007"], "2026-01-12", records={
        "30000000007": {"estado": "error", "cheques": [], "razon_social": "", "error": "BCRA no disponible"}})
    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.radio[0].set_value("Cheques por archivo").run()
    assert not app.exception
    assert app.warning
    assert not any("No se encontraron" in i.value for i in app.info)


def test_completed_no_matches_shows_empty_daily_report():
    data = daily_result(sample(), "2026-01-10")
    job = BatchJob(["30000000007"], "2026-01-10", records={"30000000007": data})
    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.radio[0].set_value("Cheques por archivo").run()
    assert not app.exception
    assert any("No se encontraron" in i.value for i in app.info)
    assert not app.get("plotly_chart")
