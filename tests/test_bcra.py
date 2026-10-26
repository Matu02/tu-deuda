from unittest.mock import Mock, patch

import pytest
import requests

from bcra import BCRAError, checks_frame, debt_frame, fetch_endpoint, fetch_report, normalize_cuit
from charts import checks_chart, debt_chart


@pytest.mark.parametrize("value", ["30532847547", "30-53284754-7", " 30532847547 "])
def test_valid_cuit(value):
    assert normalize_cuit(value) == "30532847547"


@pytest.mark.parametrize("value", ["30532847548", "abc30532847547", "123", "30/53284754/7", ""])
def test_invalid_cuit(value):
    with pytest.raises(ValueError):
        normalize_cuit(value)


def test_period_overlap_does_not_double_debt_and_chart_is_stacked():
    history = {"periodos": [
        {"periodo": "202601", "entidades": [{"entidad": "Banco A", "monto": 10}]},
        {"periodo": "202602", "entidades": [{"entidad": "Banco A", "monto": 20}]},
    ]}
    current = {"periodos": [{"periodo": "202602", "entidades": [
        {"entidad": "Banco A", "monto": 30}, {"entidad": "Banco B", "monto": 5}]}]}
    frame = debt_frame(history, current)
    assert frame.groupby("periodo")["monto_ars"].sum().to_dict() == {"202601": 10000, "202602": 35000}
    fig = debt_chart(frame)
    assert fig.layout.barmode == "stack"
    assert len(fig.data) == 2
    assert sum(sum(trace.y) for trace in fig.data) == 45000


def test_all_check_fields_and_context_preserved_and_pie_sums_amounts():
    check = {"nroCheque": 123, "monto": 1234.56, "fechaPago": None, "ctaPersonal": False,
             "enRevision": True, "campoNuevo": "conservar"}
    result = {"causales": [
        {"causal": "SIN FONDOS", "entidades": [{"entidad": 1, "detalle": [check, {**check, "monto": 10}]}]},
        {"causal": "DEFECTOS FORMALES", "entidades": [{"entidad": 2, "detalle": [{**check, "monto": 100}]}]},
    ]}
    frame = checks_frame(result)
    assert set(check) <= set(frame.columns)
    assert frame.loc[0, "fechaPago"] is None
    assert frame.loc[0, "campoNuevo"] == "conservar"
    assert frame.loc[0, "entidad"] == 1
    pie = checks_chart(frame).data[0]
    assert dict(zip(pie.labels, pie.values)) == {"SIN FONDOS": 1244.56, "DEFECTOS FORMALES": 100}


@pytest.mark.parametrize("result", [None, {}, {"causales": []}])
def test_empty_checks(result):
    assert checks_frame(result).empty


def api_response(code=200, payload=None):
    response = Mock(status_code=code)
    response.json.return_value = payload
    if code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError()
    return response


def call_with_response(response):
    with patch("bcra.requests.Session") as session:
        session.return_value.__enter__.return_value.get.return_value = response
        return fetch_endpoint("actual", "30532847547")


def test_404_is_no_records():
    assert call_with_response(api_response(404)) == {}


@pytest.mark.parametrize("payload", [[], {"status": 500}, {"status": 200, "results": {}}, "html"])
def test_bad_response_is_error(payload):
    with pytest.raises(BCRAError):
        call_with_response(api_response(payload=payload))


def test_server_error_is_not_no_debt():
    with pytest.raises(BCRAError):
        call_with_response(api_response(500))


def test_partial_failure_keeps_successful_sections():
    def fake(kind, cuit):
        if kind == "historicas":
            raise BCRAError("No disponible")
        return {"identificacion": int(cuit), "periodos": []}
    with patch("bcra.fetch_endpoint", side_effect=fake):
        report = fetch_report("30532847547")
    assert report["actual"] is not None
    assert report["historicas"] is None
    assert report["errores"] == {"historicas": "No disponible"}
