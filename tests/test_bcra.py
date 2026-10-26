from unittest.mock import Mock, patch

import pytest
import requests
import pandas as pd

from bcra import BCRAError, checks_frame, debt_frame, fetch_endpoint, fetch_report, normalize_cuit
from charts import checks_chart, debt_chart, situation_chart, SITUATIONS


def test_situation_chart_aggregates_entities_per_month_and_preserves_totals():
    frame = pd.DataFrame([
        {"periodo": "202602", "entidad": "Banco A", "situacion": 1, "monto_ars": 1000},
        {"periodo": "202602", "entidad": "Banco B", "situacion": 1, "monto_ars": 2000},
        {"periodo": "202602", "entidad": "Banco C", "situacion": 3, "monto_ars": 500},
        {"periodo": "202601", "entidad": "Banco A", "situacion": 2, "monto_ars": 700},
        {"periodo": "202601", "entidad": "Banco B", "situacion": 4, "monto_ars": 800},
        {"periodo": "202601", "entidad": "Banco C", "situacion": 5, "monto_ars": 900},
    ])
    fig = situation_chart(frame)
    assert fig.layout.barmode == "stack"
    assert list(fig.layout.xaxis.categoryarray) == ["2026-01", "2026-02"]
    assert {t.name for t in fig.data} == set(SITUATIONS.values())
    normal = next(t for t in fig.data if t.name == SITUATIONS[1])
    assert list(normal.y) == [3000]
    assert sum(sum(t.y) for t in fig.data) == frame.monto_ars.sum()


@pytest.mark.parametrize("codes", [[0, None, 6], ["1", "2", "3"], None])
def test_situation_chart_preserves_missing_and_unknown_classifications(codes):
    frame = pd.DataFrame({"periodo": ["202601"] * 3, "monto_ars": [10, 20, 30]})
    if codes is not None:
        frame["situacion"] = codes
    fig = situation_chart(frame)
    assert sum(sum(t.y) for t in fig.data) == 60
    if codes is None or codes[0] == 0:
        assert len(fig.data) == 1
        assert fig.data[0].name.startswith("Sin clasificación")


# CUIT sintético, usado sólo con respuestas simuladas.
@pytest.mark.parametrize("value", ["30000000007", "30-00000000-7", " 30000000007 "])
def test_valid_cuit(value):
    assert normalize_cuit(value) == "30000000007"


@pytest.mark.parametrize("value", ["30000000008", "abc30000000007", "123", "30/00000000/7", ""])
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
        return fetch_endpoint("actual", "30000000007")


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
        report = fetch_report("30000000007")
    assert report["actual"] is not None
    assert report["historicas"] is None
    assert report["errores"] == {"historicas": "No disponible"}
