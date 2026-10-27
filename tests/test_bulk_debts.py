import json
import threading
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from bcra_errors import BCRAError
from bulk import BatchJob, debt_result, debt_report_frames, restore_job
from test_bulk import response, run_job, synthetic_cuit


def debt_response():
    return {"denominacion": "Empresa", "periodos": [
        {"periodo": "202608", "entidades": [
            {"entidad": "Banco A", "monto": 10.25, "situacion": 1, "diasAtrasoPago": 0},
            {"entidad": "Banco B", "monto": 20, "situacion": 3}]},
        {"periodo": "202607", "entidades": [{"entidad": "Banco A", "monto": 50, "situacion": 2}]}]}


def test_debt_only_queries_current_endpoint_and_converts_units():
    cuit = synthetic_cuit(1)
    job = BatchJob([cuit, cuit], "2026-01-12", analysis="deudas")
    with patch("bulk.fetch_endpoint", return_value=debt_response()) as fetch:
        run_job(job)
    assert [c.args[:2] for c in fetch.call_args_list] == [("actual", cuit)]
    frame, summary = debt_report_frames(job.snapshot())
    assert len(frame) == 3
    assert frame.loc[frame.periodo == "202608", "monto_ars"].sum() == 30250
    assert summary.iloc[0].deuda_ars == 30250
    assert summary.iloc[0].periodo == "202608"
    assert "diasAtrasoPago" in frame
    restored = restore_job(json.dumps(job.snapshot()).encode())
    assert restored.analysis == "deudas"
    with patch("bulk.fetch_endpoint") as fetch:
        run_job(restored)
        fetch.assert_not_called()


def test_both_endpoints_for_same_company_are_inflight_together():
    barrier = threading.Barrier(2)
    calls = []
    def query(kind, cuit, **kwargs):
        calls.append((kind, cuit))
        barrier.wait(timeout=3)
        return response() if kind == "cheques" else debt_response()
    cuit = synthetic_cuit(1)
    job = BatchJob([cuit], "2026-01-12", analysis="ambos")
    with patch("bulk.fetch_endpoint", side_effect=query):
        run_job(job)
    record = job.snapshot()["resultados"][cuit]
    assert set(calls) == {("cheques", cuit), ("actual", cuit)}
    assert record["estado"] == "ok"
    assert len(record["cheques"]) == 2 and len(record["deudas"]) == 3


def test_failed_debt_resume_keeps_successful_checks():
    cuit = synthetic_cuit(2)
    job = BatchJob([cuit], "2026-01-12", analysis="ambos")
    def query(kind, *args, **kwargs):
        if kind == "actual":
            raise BCRAError("Demora")
        return response()
    with patch("bulk.fetch_endpoint", side_effect=query):
        run_job(job)
    assert job.records[cuit]["estado"] == "parcial"
    _, summary = debt_report_frames(job.snapshot())
    assert pd.isna(summary.iloc[0].deuda_ars)
    restored = restore_job(json.dumps(job.snapshot()).encode())
    with patch("bulk.fetch_endpoint", return_value=debt_response()) as fetch:
        run_job(restored)
        assert [c.args[0] for c in fetch.call_args_list] == ["actual"]
    assert restored.records[cuit]["estado"] == "ok"
    assert restored.records[cuit]["cheques"] == job.records[cuit]["cheques"]


@pytest.mark.parametrize("amount", [-1, None, "incorrecto", float("inf")])
def test_invalid_debt_never_counts_as_zero(amount):
    result = debt_response()
    result["periodos"][0]["entidades"][0]["monto"] = amount
    with pytest.raises((BCRAError, ValueError)):
        debt_result(result)


def test_checkpoint_rejects_invalid_debt_and_mode():
    cuit = synthetic_cuit(3)
    job = BatchJob([cuit], "2026-01-12", analysis="deudas")
    with patch("bulk.fetch_endpoint", return_value=debt_response()):
        run_job(job)
    data = job.snapshot()
    data["resultados"][cuit]["deudas"][0]["monto_ars"] = -1
    with pytest.raises(ValueError):
        restore_job(json.dumps(data).encode())
    with pytest.raises(ValueError):
        BatchJob([cuit], "2026-01-12", analysis="otro")


def test_debt_dashboard_selects_period_instead_of_adding_balances():
    app = AppTest.from_file("app.py").run()
    cuit = synthetic_cuit(4)
    job = BatchJob([cuit], "2026-01-12", analysis="deudas")
    with patch("bulk.fetch_endpoint", return_value=debt_response()):
        run_job(job)
    app.session_state["bulk_job"] = job
    app.run()
    assert not app.exception
    assert next(m.value for m in app.metric if m.label == "Deuda total del período (ARS)") == "$ 30.250,00"
    assert not any(m.label == "Cheques rechazados" for m in app.metric)
    period = next(s for s in app.selectbox if s.label == "Período de deuda a analizar")
    period.set_value("202607").run()
    assert not app.exception
    assert next(m.value for m in app.metric if m.label == "Deuda total del período (ARS)") == "$ 50.000,00"
    downloads = {d.proto.label for d in app.get("download_button")}
    assert {"Descargar todas las deudas (Excel)", "Descargar todas las deudas (CSV)"} <= downloads


def test_debt_only_upload_does_not_require_checks_catalog():
    from unittest.mock import Mock
    uploaded = Mock()
    uploaded.name = "clientes.csv"
    uploaded.getvalue.return_value = f"CUIT\n{synthetic_cuit(5)}\n".encode()
    app = AppTest.from_file("app.py").run()
    next(s for s in app.selectbox if s.label == "Análisis a solicitar").set_value("Deudas")
    with patch("streamlit.file_uploader", side_effect=lambda label, **kwargs: None if "avance" in label else uploaded), patch("bulk_ui._catalog") as catalog, patch.object(BatchJob, "start"):
        app.run()
        next(b for b in app.button if b.label == "Iniciar nuevo lote").click().run()
        assert not app.exception
        catalog.assert_not_called()
    assert app.session_state["bulk_job"].analysis == "deudas"
