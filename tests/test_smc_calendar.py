from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from teamtrader import agents, data, economic_calendar, history, pipeline, smc
from teamtrader.config import ASSETS

from test_teamtrader import FakeClient, make_ohlcv, text


def path(points: list[tuple[float, int]]) -> pd.DataFrame:
    """Bougies 1h qui suivent un chemin : liste de (prix, nombre de bougies pour y arriver)."""
    xs = [points[0][0]]
    for (a, _), (b, n) in zip(points, points[1:]):
        xs += list(np.linspace(a, b, n + 1)[1:])
    c = np.array(xs)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range("2026-01-01", periods=len(c), freq="1h", tz="UTC")
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) + 0.3,
                         "Low": np.minimum(o, c) - 0.3, "Close": c, "Volume": 0}, index=idx)


# Baisse, plus bas à 100, cassure haussière, repli interne court (l'inducement), plus haut à 128.
BULL_LEG = [(130, 1), (110, 12), (118, 12), (100, 12), (120, 12), (115, 12), (123, 8),
            (121.5, 3), (128, 6)]


def setup_after(end):
    return smc.analyze(path(BULL_LEG + end), "1h").setup


# ------------------------------------------------------------------ détection SMC


def test_swings_and_structure_breaks():
    df = path(BULL_LEG)
    swings = smc.find_swings(df, smc.MAJOR_WINDOW)
    assert any(s.kind == "low" and abs(s.price - 99.7) < 1e-6 for s in swings)
    trend, breaks = smc.market_structure(df, swings, smc.MAJOR_WINDOW)
    assert trend == "haussier"
    kinds = [(b.kind, b.direction) for b in breaks]
    assert ("CHoCH", "haussier") in kinds and kinds[-1] == ("BOS", "haussier")


def test_fvg_detection():
    df = pd.DataFrame(
        {"Open": [10, 11, 15, 15], "High": [11, 16, 17, 16], "Low": [9.5, 10.5, 13, 14],
         "Close": [11, 15, 16, 15], "Volume": 0},
        index=pd.date_range("2026-01-01", periods=4, freq="1h", tz="UTC"),
    )
    [fvg] = smc.find_fvgs(df, min_size=0.5)
    assert (fvg.direction, fvg.bottom, fvg.top) == ("haussier", 11, 13)
    assert fvg.state == "ouvert"


def test_entry_not_allowed_until_inducement_is_swept():
    s = setup_after([(124, 4)])
    assert s.direction == "haussier"
    assert s.idm_level == pytest.approx(121.2)
    assert not s.idm_swept and not s.entry_allowed
    assert "inducement non balayé" in s.status
    # Le plan existe déjà (POI sous l'inducement, en discount) mais reste conditionnel.
    assert s.poi is not None and s.poi.top <= s.idm_level and s.poi_in_discount
    assert s.stop < s.entry < s.targets[0]


def test_entry_allowed_once_inducement_is_swept():
    s = setup_after([(120, 6)])
    assert s.idm_swept and s.entry_allowed
    assert s.status.startswith("validé")
    assert s.entry - s.stop >= 0.5 * smc.analyze(path(BULL_LEG + [(120, 6)]), "1h").atr - 1e-9


def test_breaking_leg_origin_flips_structure():
    s = setup_after([(113, 10)])
    assert s.direction == "baissier" and not s.entry_allowed


def test_bearish_mirror():
    mirrored = [(200 - p, n) for p, n in BULL_LEG] + [(200 - 120, 6)]
    s = smc.analyze(path(mirrored), "1h").setup
    assert s.direction == "baissier" and s.entry_allowed
    assert s.targets[0] < s.entry < s.stop


def test_format_map_mentions_rule():
    txt = smc.format_map(smc.analyze(path(BULL_LEG + [(124, 4)]), "1h"))
    assert "Entrée autorisée par la règle d'inducement : NON" in txt
    assert "Inducement : 121.20" in txt


# ------------------------------------------------------------------ règle appliquée par le code


def decision(kind="ACHAT"):
    return agents.TradingDecision(
        decision=kind, confiance=70, horizon="2 jours", synthese="Plan.", prix_entree=115.7,
        zone_entree="OB 4h", stop_loss=114.5, take_profits=[128.3], vues_agents=[],
        arguments_pour=[], arguments_contre=[], conditions_invalidation=[],
        evenements_a_surveiller=[],
    )


def test_rule_blocks_entry_before_inducement():
    d, reason = pipeline.enforce_inducement_rule(decision(), setup_after([(124, 4)]))
    assert d.decision == "ATTENDRE" and "121.20" in reason
    assert d.prix_entree == 115.7 and d.zone_entree.startswith("Conditionnel")


def test_rule_blocks_trade_against_validated_setup():
    d, reason = pipeline.enforce_inducement_rule(decision("VENTE"), setup_after([(120, 6)]))
    assert d.decision == "ATTENDRE" and "contre le setup" in reason


def test_rule_lets_valid_entry_through():
    d, reason = pipeline.enforce_inducement_rule(decision(), setup_after([(120, 6)]))
    assert d.decision == "ACHAT" and reason is None


def test_decide_in_smc_mode_enforces_rule_and_sends_rule_to_model():
    df = path(BULL_LEG + [(124, 4)])
    m = smc.analyze(df, "1h")
    ctx = pipeline.Context(asset=ASSETS["XAUUSD"], style="swing", frames={"1h": df},
                           price=124.0, technical="tech", macro="", smc_map=m,
                           calendar="| NFP |")
    client = FakeClient([NS(stop_reason="end_turn", parsed_output=decision(), content=[])])
    d = pipeline.decide(client, ctx, [], calendar_forecast="NFP au-dessus")
    assert d.decision == "ATTENDRE"
    prompt = client.calls[0]["messages"][0]["content"]
    assert "MODE SMART MONEY CONCEPTS" in prompt and "Inducement : 121.20" in prompt
    assert "| NFP |" in prompt and "NFP au-dessus" in prompt
    assert [s.key for s in ctx.agent_specs][-1] == "smc"
    assert "Cartographie Smart Money" in ctx.briefing("smc")


# ------------------------------------------------------------------ calendrier


RAW = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-03-06T08:30:00-05:00",
     "impact": "High", "forecast": "180K", "previous": "151K"},
    {"title": "CPI m/m", "country": "USD", "date": "2026-03-04T08:30:00-05:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
    {"title": "ECB Press Conference", "country": "EUR", "date": "2026-03-05T08:45:00-05:00",
     "impact": "High", "forecast": "", "previous": ""},
    {"title": "Crude Oil Inventories", "country": "USD", "date": "2026-03-04T10:30:00-05:00",
     "impact": "Low", "forecast": "", "previous": ""},
    {"title": "Bad date", "country": "USD", "date": "demain", "impact": "High"},
]


def test_parse_events_keeps_important_us_releases_in_utc_order():
    events = economic_calendar.parse_events(RAW)
    assert [e.title for e in events] == ["CPI m/m", "Non-Farm Employment Change"]
    assert events[0].time == datetime(2026, 3, 4, 13, 30, tzinfo=timezone.utc)
    assert events[1].impact == "forte" and events[1].forecast == "180K"


def test_upcoming_and_format_in_paris_time():
    events = economic_calendar.parse_events(RAW)
    now = datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc)
    assert [e.title for e in economic_calendar.upcoming(events, 48, now)] == [
        "Non-Farm Employment Change"]
    table = economic_calendar.format_events(events, "Europe/Paris")
    assert "ven. 06/03 14:30" in table and "180K" in table  # 13:30 UTC = 14:30 à Paris en mars


def test_safe_calendar_text_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(data, "_get_json", boom)
    assert "indisponible" in economic_calendar.safe_calendar_text()


def test_forecast_calendar_two_steps():
    forecast = agents.CalendarForecast(
        synthese="Semaine chargée.", gestion_du_risque="Réduire la taille avant le NFP.",
        evenements=[agents.EventForecast(
            evenement="NFP", date_heure_utc="2026-03-06 13:30", importance="forte",
            precedent="151K", consensus="180K", prevision="EN DESSOUS", estimation="150K",
            probabilite=60, indicateurs_avances=["ADP faible"], reaction_usd="BAISSIER",
            reaction_or="HAUSSIER", reaction_btc="HAUSSIER", explication="...",
        )],
    )
    client = FakeClient([
        NS(stop_reason="end_turn", content=[text("Note de l'économiste : NFP en dessous.")]),
        NS(stop_reason="end_turn", parsed_output=forecast, content=[]),
    ])
    out, report = agents.forecast_calendar(client, "| NFP |")
    assert out is forecast and "NFP en dessous" in report.text
    assert client.calls[0]["tools"][0]["name"] == "web_search"
    assert client.calls[1]["output_format"] is agents.CalendarForecast
    assert "NFP en dessous" in client.calls[1]["messages"][0]["content"]


# ------------------------------------------------------------------ historique


def test_old_history_database_gets_mode_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(history.SCHEMA.replace(",\n    mode TEXT NOT NULL DEFAULT 'standard'", ""))
    con.close()
    con = history.connect(str(db))
    history.save(con, asset="XAUUSD", style="Swing (quelques jours)", price=1.0,
                 decision=decision(), reports=[], mode="smc")
    assert history.load(con)[0].mode == "smc"


def test_price_history_with_15m(monkeypatch):
    h = make_ohlcv(n=200)
    monkeypatch.setattr(data, "fetch_ohlcv", lambda ticker, period="60d", interval="1h",
                        start=None: h)
    monkeypatch.setattr(data.config, "TWELVEDATA_API_KEY", "")
    p = data.get_price_history(ASSETS["BTCUSD"], with_15m=True)
    assert list(p.frames) == ["15m", "1h", "4h", "1d"]
