from unittest.mock import patch

from streamlit.testing.v1 import AppTest


def report_fixture():
    return {
        "cuit": "30000000007", "consultado": "2026-10-25T23:00:00+00:00", "errores": {},
        "actual": {"denominacion": "EMPRESA DE PRUEBA", "periodos": [
            {"periodo": "202608", "entidades": [{"entidad": "Banco A", "monto": 10}]}]},
        "historicas": {"periodos": [
            {"periodo": "202607", "entidades": [{"entidad": "Banco B", "monto": 5}]}]},
        "cheques": {"causales": [{"causal": "SIN FONDOS", "entidades": [
            {"entidad": 1, "detalle": [{"nroCheque": 123, "monto": 500.25, "fechaPago": None}]}]}]},
    }


def test_submit_renders_both_charts_and_full_table():
    app = AppTest.from_file("app.py").run()
    assert not app.exception
    assert app.text_input[0].value == ""
    with patch("bcra.fetch_report", return_value=report_fixture()):
        app.text_input[0].set_value("30000000007")
        app.button[0].click().run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 2
    assert len(app.dataframe) == 2
    assert "fechaPago" in app.dataframe[1].value.columns
    assert app.metric[0].value == "$ 10.000,00"


def test_invalid_cuit_does_not_call_bcra():
    app = AppTest.from_file("app.py").run()
    with patch("bcra.fetch_report") as fetch:
        app.text_input[0].set_value("123").run()
        app.button[0].click().run()
    assert not app.exception
    assert app.error
    fetch.assert_not_called()


def test_partial_and_empty_states():
    app = AppTest.from_file("app.py").run()
    data = report_fixture()
    data.update(actual=None, historicas={}, cheques={})
    data["errores"] = {"actual": "Demora del BCRA"}
    app.session_state["report"] = data
    app.run()
    assert not app.exception
    assert app.warning
    assert not app.get("plotly_chart")
    assert app.metric[0].value == "No disponible"


def test_partial_submission_can_be_retried():
    import streamlit as st
    st.cache_data.clear()
    app = AppTest.from_file("app.py").run()
    data = report_fixture()
    data["historicas"] = None
    data["errores"] = {"historicas": "BCRA no disponible"}
    with patch("bcra.fetch_report", return_value=data) as fetch:
        app.text_input[0].set_value("30000000007")
        app.button[0].click().run()
        assert not app.exception
        assert app.warning
        app.text_input[0].set_value("30000000007")
        app.button[0].click().run()
        assert fetch.call_count == 2
    assert not app.exception
    assert len(app.get("plotly_chart")) == 2


def test_no_records_message():
    app = AppTest.from_file("app.py").run()
    data = report_fixture()
    data.update(actual={}, historicas={}, cheques={})
    app.session_state["report"] = data
    app.run()
    assert not app.exception
    assert "no devolvió registros" in app.info[0].value
    assert not app.get("plotly_chart")
