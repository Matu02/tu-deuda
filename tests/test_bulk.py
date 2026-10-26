from datetime import date
from io import BytesIO
import json
import threading
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from bcra import BCRACancelled, BCRACooldown, BCRARateLimit, RequestGate, _get_payload
from bulk import BatchJob, daily_result, extract_cuits, read_table, report_frames, restore_job
from charts import companies_chart


def synthetic_cuit(number):
    body = f"30{number:08d}"
    check = 11 - sum(int(n) * w for n, w in zip(body, [5, 4, 3, 2, 7, 6, 5, 4, 3, 2])) % 11
    return body + str(0 if check == 11 else 9 if check == 10 else check)


def response(name="Empresa sintética"):
    return {"denominacion": name, "causales": [{"causal": "SIN FONDOS", "entidades": [
        {"entidad": 11, "detalle": [
            {"nroCheque": 1, "monto": 100.25, "fechaRechazo": "2026-01-12", "fechaPago": None, "extra": "conservar"},
            {"nroCheque": 2, "monto": 500, "fechaRechazo": "2026-01-11", "fechaPago": None},
        ]}]}]}


@pytest.mark.parametrize("separator", [",", ";", "\t"])
def test_csv_delimiters_and_numeric_cuits(separator):
    cuit = synthetic_cuit(1)
    source = f"CUIT{separator}Cliente\n{cuit}{separator}Cliente de prueba\n"
    table = read_table(source.encode(), "clientes.csv")
    cuits, invalid, duplicates, blanks = extract_cuits(table)
    assert cuits == [cuit] and invalid.empty and duplicates == 0 and blanks == 0


def test_single_column_csv_not_split_on_digits():
    cuit = synthetic_cuit(2)
    table = read_table(f"CUIT\n{cuit}\n".encode(), "clientes.csv")
    assert table.shape == (2, 1)
    assert extract_cuits(table)[0] == [cuit]


def test_excel_custom_sheet_row_and_column_dedup_and_invalids():
    cuit = synthetic_cuit(3)
    formatted = f"{cuit[:2]}-{cuit[2:10]}-{cuit[-1]}"
    source = pd.DataFrame([["Introducción", None], ["Cliente", "CUIT"],
                           ["A", int(cuit)], ["A", formatted], ["B", "inválido"], ["C", None]])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        source.to_excel(writer, index=False, header=False, sheet_name="Clientes")
    table = read_table(output.getvalue(), "clientes.xlsx", "Clientes")
    cuits, invalid, duplicates, blanks = extract_cuits(table, start_row=3, column=1)
    assert cuits == [cuit] and duplicates == 1 and blanks == 1
    assert invalid.iloc[0]["fila"] == 5


def test_daily_filter_keeps_full_fields_and_flags_unknown_dates():
    result = response()
    result["causales"][0]["entidades"][0]["detalle"].append({"monto": 42, "fechaRechazo": None})
    daily = daily_result(result, "2026-01-12")
    assert daily["estado"] == "parcial" and daily["fechas_invalidas"] == 1
    assert len(daily["cheques"]) == 1
    assert daily["cheques"][0]["extra"] == "conservar"
    assert daily["cheques"][0]["monto"] == 100.25
    assert daily["razon_social"] == "Empresa sintética"
    assert daily_result({}, "2026-01-12")["cheques"] == []


def run_job(job):
    job.stop_event.wait = Mock(return_value=False)
    job.start()
    job.thread.join(timeout=5)
    assert not job.thread.is_alive()


def test_3001_cuits_only_cheques_one_call_each_and_no_duplicate_on_resume():
    cuits = [synthetic_cuit(i) for i in range(3001)]
    job = BatchJob(cuits, date(2026, 1, 12))
    with patch("bulk.fetch_endpoint", return_value=response()) as fetch:
        run_job(job)
        assert fetch.call_count == 3001
        assert all(c.args[0] == "cheques" for c in fetch.call_args_list)
        restored = restore_job(json.dumps(job.snapshot()).encode())
        run_job(restored)
        assert fetch.call_count == 3001
    frame, summary = report_frames(restored.snapshot(), {11: "Banco sintético"})
    assert len(frame) == 3001 and len(summary) == 3001
    assert frame.CUIT.nunique() == 3001
    assert frame.nombreEntidad.eq("Banco sintético").all()
    assert frame.razon_social.eq("Empresa sintética").all()
    assert frame.monto.sum() == pytest.approx(3001 * 100.25)


@pytest.mark.parametrize("error", [BCRARateLimit("HTTP 429"), BCRACooldown("HTTP 503: esperar")])
def test_server_pause_stops_batch_and_resume_does_not_skip_failed_company(error):
    cuits = [synthetic_cuit(1), synthetic_cuit(2)]
    job = BatchJob(cuits, "2026-01-12")
    with patch("bulk.fetch_endpoint", side_effect=error) as fetch:
        run_job(job)
        assert fetch.call_count == 1
    snapshot = job.snapshot()
    assert snapshot["resultados"][cuits[0]]["estado"] == "error"
    assert cuits[1] not in snapshot["resultados"]
    _, summary = report_frames(snapshot)
    assert summary["monto_total"].isna().all()
    restored = restore_job(json.dumps(snapshot).encode())
    with patch("bulk.fetch_endpoint", return_value=response()) as fetch:
        run_job(restored)
        assert fetch.call_count == 2
    assert all(r["estado"] == "ok" for r in restored.snapshot()["resultados"].values())


def test_checkpoint_rejects_wrong_day_or_bad_format():
    cuit = synthetic_cuit(1)
    job = BatchJob([cuit], "2026-01-12", records={cuit: daily_result(response(), "2026-01-12")})
    data = job.snapshot()
    data["fecha"] = "2026-01-13"
    with pytest.raises(ValueError):
        restore_job(json.dumps(data).encode())
    with pytest.raises(ValueError):
        restore_job(b'not-json')


def test_top10_companies_group_by_cuit_and_rest_preserves_total():
    frame = pd.DataFrame([{"CUIT": synthetic_cuit(i), "razon_social": "Mismo nombre", "monto": i + 1} for i in range(12)])
    pie = companies_chart(frame).data[0]
    assert len(pie.labels) == 11
    assert "Resto de empresas" in pie.labels
    assert sum(pie.values) == frame.monto.sum()
    assert synthetic_cuit(11) in pie.labels[0]


def test_request_gate_waits_and_honors_shared_cooldown():
    now = [0.0]
    gate = RequestGate(interval=1)
    with patch("bcra.time.monotonic", side_effect=lambda: now[0]), patch("bcra.time.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
        gate.wait()
        gate.wait()
        assert now[0] == 1
        gate.defer(12)
        gate.wait()
        assert now[0] == 13
    event = threading.Event()
    event.set()
    with pytest.raises(BCRACancelled):
        gate.wait(event)


def test_http429_respects_retry_after_before_retrying():
    limited = Mock(status_code=429, headers={"Retry-After": "17"})
    success = Mock(status_code=200)
    success.json.return_value = {"status": 200}
    with patch("bcra._REQUEST_GATE") as gate, patch("bcra.requests.Session") as session:
        session.return_value.__enter__.return_value.get.side_effect = [limited, success]
        assert _get_payload("https://api.bcra.gob.ar/test")["status"] == 200
        gate.defer.assert_called_once_with(17)
        assert gate.wait.call_count == 2


def test_http503_with_long_retry_after_pauses_without_retrying():
    response = Mock(status_code=503, headers={"Retry-After": "3743"})
    with patch("bcra._REQUEST_GATE") as gate, patch("bcra.requests.Session") as session:
        session.return_value.__enter__.return_value.get.return_value = response
        with pytest.raises(BCRACooldown, match="503.*3743"):
            _get_payload("https://api.bcra.gob.ar/test")
        gate.defer.assert_called_once_with(3743)
        assert session.return_value.__enter__.return_value.get.call_count == 1


def test_http503_with_short_retry_after_retries_after_wait():
    response = Mock(status_code=503, headers={"Retry-After": "5"})
    success = Mock(status_code=200)
    success.json.return_value = {"status": 200}
    with patch("bcra._REQUEST_GATE") as gate, patch("bcra.requests.Session") as session:
        session.return_value.__enter__.return_value.get.side_effect = [response, success]
        assert _get_payload("https://api.bcra.gob.ar/test")["status"] == 200
        gate.defer.assert_called_once_with(5)
        assert gate.wait.call_count == 2


def test_long_shared_cooldown_returns_immediately_without_sleep_or_request():
    gate = RequestGate()
    with patch("bcra.time.sleep") as sleep:
        gate.defer(3743)
        with pytest.raises(BCRACooldown):
            gate.wait()
        sleep.assert_not_called()


def test_cancelled_inflight_batch_keeps_result_and_does_not_start_next():
    job = BatchJob([synthetic_cuit(1), synthetic_cuit(2)], "2026-01-12")
    def query(*args, **kwargs):
        job.pause()
        return response()
    with patch("bulk.fetch_endpoint", side_effect=query) as fetch:
        run_job(job)
        assert fetch.call_count == 1
    assert len(job.snapshot()["resultados"]) == 1
