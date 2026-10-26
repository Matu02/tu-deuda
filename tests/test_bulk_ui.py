from datetime import date
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from bulk import BatchJob, checks_result


def sample():
    return {"denominacion": "Empresa de prueba", "causales": [{"causal": "SIN FONDOS", "entidades": [
        {"entidad": 11, "detalle": [
            {"nroCheque": 1, "monto": 1234567.89, "fechaRechazo": "2026-01-12"},
            {"nroCheque": 2, "monto": 9999999, "fechaRechazo": "2026-01-11"},
        ]}]}]}


def test_upload_keeps_all_rejection_dates_without_any_debt_requests():
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
        assert not app.exception
        next(b for b in app.button if b.label == "Iniciar nuevo lote").click().run()
    assert not app.exception
    assert not app.date_input
    assert fetch.call_count == 1 and fetch.call_args.args[0] == "cheques"
    assert [m.value for m in app.metric] == ["1", "$ 11.234.566,89", "2"]
    assert len(app.get("plotly_chart")) == 2
    detail = next(d.value for d in app.dataframe if "razon_social" in d.value and "nroCheque" in d.value)
    assert len(detail) == 2
    assert detail.loc[0, "CUIT"] == "30000000007"
    assert detail.loc[0, "razon_social"] == "Empresa de prueba"
    assert detail.loc[0, "nombreEntidad"] == "Banco de prueba"


def test_failed_or_pending_cuits_never_claim_a_complete_empty_report():
    job = BatchJob(["30000000007"], "2026-01-12", records={
        "30000000007": {"estado": "error", "cheques": [], "razon_social": "", "error": "BCRA no disponible"}})
    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.run()
    assert not app.exception
    assert app.warning
    assert not any("no informó" in i.value for i in app.info)


def test_completed_no_records_shows_empty_report():
    data = checks_result({})
    job = BatchJob(["30000000007"], "2026-01-10", records={"30000000007": data})
    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.run()
    assert not app.exception
    assert any("no informó" in i.value for i in app.info)
    assert not app.get("plotly_chart")


def test_catalog_503_does_not_start_company_queries():
    import streamlit as st
    from bcra_errors import BCRAUnavailable
    st.cache_data.clear()
    uploaded = Mock()
    uploaded.name = "clientes.csv"
    uploaded.getvalue.return_value = b"CUIT\n30000000007\n"
    def upload(label, **kwargs):
        return None if "avance" in label else uploaded
    with patch("streamlit.file_uploader", side_effect=upload), patch("bcra.fetch_entities", side_effect=BCRAUnavailable()), patch("bulk.fetch_endpoint") as fetch:
        app = AppTest.from_file("app.py").run()
        next(b for b in app.button if b.label == "Iniciar nuevo lote").click().run()
        assert not app.exception
        assert any("Caída la API BCRA" in e.value for e in app.error)
        fetch.assert_not_called()
    st.cache_data.clear()


def test_503_deadline_disables_resume_button():
    from datetime import datetime, timezone, timedelta
    job = BatchJob(["30000000007"], "2026-01-12")
    job.retry_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    app = AppTest.from_file("app.py").run()
    app.session_state["bulk_job"] = job
    app.run()
    assert not app.exception
    assert next(b for b in app.button if b.label == "Continuar / reintentar pendientes").disabled
    assert any("Caída la API BCRA" in e.value for e in app.error)
