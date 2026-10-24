from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from teamtrader import agents, calendar_history as ch, economic_calendar

from test_teamtrader import FakeClient, text

T_NFP = datetime(2026, 3, 6, 13, 30, tzinfo=timezone.utc)
T_CPI = datetime(2026, 3, 4, 13, 30, tzinfo=timezone.utc)


def ev(name, when, prev, cons, prediction, proba=60, gold="HAUSSIER", btc="HAUSSIER"):
    return agents.EventForecast(
        evenement=name, date_heure_utc=f"{when:%Y-%m-%d %H:%M}", importance="forte",
        precedent=prev, consensus=cons, prevision=prediction, estimation="?",
        probabilite=proba, indicateurs_avances=[], reaction_usd="NEUTRE", reaction_or=gold,
        reaction_btc=btc, explication="",
    )


def forecast(*events):
    return agents.CalendarForecast(synthese="", gestion_du_risque="", evenements=list(events))


@pytest.mark.parametrize("actual,consensus,expected", [
    ("180K", "180K", "CONFORME"),
    ("181K", "180K", "AU-DESSUS"),
    ("0.2%", "0.3%", "EN DESSOUS"),
    ("0.30%", "0.3%", "CONFORME"),
    ("1.2M", "1150K", "AU-DESSUS"),
    ("-0,1 %", "0.0%", "EN DESSOUS"),
    ("introuvable", "180K", None),
    ("200K", "n/d", None),
])
def test_outcome_vs_consensus(actual, consensus, expected):
    assert ch.outcome(actual, consensus) == expected


def test_save_load_and_keep_last_forecast_before_release(tmp_path):
    con = ch.connect(str(tmp_path / "h.db"))
    ch.save_forecast(con, forecast(ev("Non-Farm Employment Change", T_NFP, "151K", "180K",
                                      "AU-DESSUS")), created_at=T_NFP - timedelta(days=3))
    ch.save_forecast(con, forecast(ev("Non-Farm Employment Change", T_NFP, "151K", "180K",
                                      "EN DESSOUS")), created_at=T_NFP - timedelta(days=1))
    # Prévision faite APRÈS la publication : ignorée.
    ch.save_forecast(con, forecast(ev("Non-Farm Employment Change", T_NFP, "151K", "180K",
                                      "AU-DESSUS")), created_at=T_NFP + timedelta(hours=1))
    rows = ch.load(con)
    assert len(rows) == 3 and rows[0].event_time == T_NFP
    [kept] = ch.latest_per_event(rows)
    assert kept.prediction == "EN DESSOUS"
    assert [r.id for r in ch.pending_actuals(rows)] == [kept.id]
    ch.set_actual(con, kept.id, "150K", "manuel")
    assert ch.pending_actuals(ch.load(con)) == []


def candles_around(t, open_, close, n=6):
    idx = pd.date_range(t.replace(minute=0) - timedelta(hours=2), periods=n, freq="1h", tz="UTC")
    o = np.full(n, open_)
    c = np.full(n, open_)
    c[2] = close  # la bougie de la publication
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": 0}, index=idx)


def test_evaluate_prediction_and_market_reaction(tmp_path):
    con = ch.connect(str(tmp_path / "h.db"))
    ch.save_forecast(con, forecast(
        ev("Non-Farm Employment Change", T_NFP, "151K", "180K", "EN DESSOUS", 62,
           gold="HAUSSIER", btc="HAUSSIER"),
        ev("CPI m/m", T_CPI, "0.4%", "0.3%", "AU-DESSUS", 70, gold="BAISSIER", btc="BAISSIER"),
    ), created_at=T_CPI - timedelta(days=2))
    for r in ch.load(con):
        ch.set_actual(con, r.id, "140K" if r.event.startswith("Non") else "0.3%", "test")
    candles = {"XAUUSD": pd.concat([candles_around(T_CPI, 2000, 1990),
                                    candles_around(T_NFP, 2000, 2015)]),
               "BTCUSD": candles_around(T_NFP, 60000, 60050)}
    evals = {e.row.event: e for e in ch.evaluate(ch.load(con), candles)}

    nfp = evals["Non-Farm Employment Change"]
    assert (nfp.outcome, nfp.correct) == ("EN DESSOUS", True)
    assert nfp.gold_move == pytest.approx(0.75) and nfp.gold_ok is True
    assert nfp.btc_ok is False  # +0,08 % < seuil de 0,25 % : réaction neutre, pas haussière

    cpi = evals["CPI m/m"]
    assert (cpi.outcome, cpi.correct) == ("CONFORME", False)
    assert cpi.gold_ok is True and cpi.btc_move is None

    s = ch.statistics(list(evals.values()))
    assert s["prevision"] == {"n": 2, "justes": 1, "taux": 50.0}
    assert s["par_probabilite"]["≥ 65 %"]["justes"] == 0
    assert s["reaction_or"]["taux"] == 100.0


def test_track_record_needs_enough_history():
    def e(ok, proba):
        return ch.Evaluation(NS(prediction="AU-DESSUS", probability=proba), "X", ok)
    assert ch.track_record_text(ch.statistics([e(True, 70)] * 4)) == ""
    txt = ch.track_record_text(ch.statistics([e(True, 70), e(False, 70)] * 3))
    assert "3/6 (50 %)" in txt and "≥ 65 %" in txt


def test_match_feed_actuals(tmp_path):
    con = ch.connect(str(tmp_path / "h.db"))
    ch.save_forecast(con, forecast(ev("Non-Farm Employment Change", T_NFP, "151K", "180K",
                                      "AU-DESSUS")), created_at=T_NFP - timedelta(days=1))
    feed = economic_calendar.parse_events([
        {"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
         "date": "2026-03-06T08:30:00-05:00", "forecast": "180K", "previous": "151K",
         "actual": "212K"},
        {"title": "Unemployment Rate", "country": "USD", "impact": "High",
         "date": "2026-03-06T08:30:00-05:00", "forecast": "4.1%", "actual": "4.0%"},
    ])
    rows = ch.load(con)
    assert ch.match_feed_actuals(rows, feed) == {rows[0].id: "212K"}


def test_fetch_actuals_two_steps_and_ignores_unknown_ids():
    parsed = agents.ActualValues(resultats=[agents.ActualValue(id=7, publie="212K"),
                                            agents.ActualValue(id=99, publie="1")])
    client = FakeClient([
        NS(stop_reason="end_turn", content=[text("7. NFP : 212K selon le BLS")]),
        NS(stop_reason="end_turn", parsed_output=parsed, content=[]),
    ])
    found, report = agents.fetch_actuals(client, [
        {"id": 7, "evenement": "NFP", "date_heure_utc": "2026-03-06 13:30", "consensus": "180K"}])
    assert found == {7: "212K"}
    assert "7. NFP" in client.calls[0]["messages"][0]["content"]


def test_track_record_is_sent_to_economist():
    client = FakeClient([NS(stop_reason="end_turn", content=[])])
    agents.forecast_calendar(client, "| NFP |", track_record="- 3/6 (50 %)")
    assert "Ta fiabilité mesurée" in client.calls[0]["messages"][0]["content"]
