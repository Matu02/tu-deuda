from unittest.mock import patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from bcra import BCRAError


@pytest.fixture(autouse=True)
def mock_entities():
    st.cache_data.clear()
    with patch("bcra.fetch_entities", return_value={11: "Banco de prueba"}):
        yield


def report_fixture():
    return {
        "cuit": "30000000007", "consultado": "2026-10-25T23:00:00+00:00", "errores": {},
        "actual": {"denominacion": "EMPRESA DE PRUEBA", "periodos": [
            {"periodo": "202608", "entidades": [{"entidad": "Banco A", "monto": 10, "situacion": 1}]}]},
        "historicas": {"periodos": [
            {"periodo": "202607", "entidades": [{"entidad": "Banco B", "monto": 5, "situacion": 2}]}]},
        "cheques": {"causales": [{"causal": "SIN FONDOS", "entidades": [
            {"entidad": 1, "detalle": [{"nroCheque": 123, "monto": 500.25, "fechaPago": None}]}]}]},
    }


def test_submit_renders_three_charts_and_full_table():
    app = AppTest.from_file("app.py").run()
    assert not app.exception
    assert app.text_input[0].value == ""
    with patch("bcra.fetch_report", return_value=report_fixture()):
        app.text_input[0].set_value("30000000007")
        app.button[0].click().run()
    assert not app.exception
    assert len(app.get("plotly_chart")) == 3
    assert len(app.dataframe) == 2
    assert "fechaPago" in app.dataframe[1].value.columns
    assert app.dataframe[1].value.loc[0, "nombreEntidad"] == "Entidad no identificada (código 1)"
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
    assert len(app.get("plotly_chart")) == 3


def test_no_records_message():
    app = AppTest.from_file("app.py").run()
    data = report_fixture()
    data.update(actual={}, historicas={}, cheques={})
    app.session_state["report"] = data
    app.run()
    assert not app.exception
    assert "no devolvió registros" in app.info[0].value
    assert not app.get("plotly_chart")


def test_catalog_failure_keeps_cheques_and_can_retry():
    app = AppTest.from_file("app.py").run()
    data = report_fixture()
    data["cheques"]["causales"][0]["entidades"][0]["entidad"] = 11
    app.session_state["report"] = data
    with patch("bcra.fetch_entities", side_effect=BCRAError("No disponible")):
        app.run()
    assert not app.exception
    assert app.warning
    assert app.dataframe[1].value.loc[0, "entidad"] == 11
    assert len(app.get("plotly_chart")) == 3
    app.run()
    assert not app.exception
    assert app.dataframe[1].value.loc[0, "nombreEntidad"] == "Banco de prueba"
