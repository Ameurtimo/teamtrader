from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from teamtrader import agents, config, data, history
from teamtrader.config import ASSETS

from test_teamtrader import make_ohlcv

T0 = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)


def candles(path: list[tuple[float, float, float]]) -> pd.DataFrame:
    """Bougies 1h à partir de T0 + 1h : liste de (bas, haut, clôture)."""
    idx = pd.date_range(T0 + timedelta(hours=1), periods=len(path), freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "Open": [c for _, _, c in path], "High": [h for _, h, _ in path],
            "Low": [lo for lo, _, _ in path], "Close": [c for _, _, c in path], "Volume": 0,
        },
        index=idx,
    )


def rec(decision="ACHAT", entry=2000.0, stop=1980.0, targets=(2040.0,), price=2000.0,
        max_hours=240, signals=None):
    return history.Record(
        id=1, created_at=T0, asset="XAUUSD", style="Swing (quelques jours)",
        max_hours=max_hours, price=price, decision=decision, confidence=60, entry=entry,
        stop=stop, targets=list(targets), horizon="", signals=signals or {},
    )


LATER = T0 + timedelta(days=30)


def test_long_hits_target():
    o = history.evaluate(rec(), candles([(1995, 2010, 2005), (2005, 2045, 2041)]), now=LATER)
    assert o.status == "gagnant"
    assert o.r_multiple == pytest.approx(2.0)


def test_short_hits_stop():
    r = rec(decision="VENTE", stop=2020, targets=(1960,))
    o = history.evaluate(r, candles([(1995, 2010, 2005), (2005, 2025, 2022)]), now=LATER)
    assert o.status == "perdant" and o.r_multiple == -1.0


def test_same_candle_touching_stop_and_target_counts_as_loss():
    o = history.evaluate(rec(), candles([(1975, 2045, 2000)]), now=LATER)
    assert o.status == "perdant"


def test_limit_entry_waits_for_fill():
    r = rec(entry=1990, stop=1970, targets=(2030,))
    never = candles([(1995, 2010, 2005)] * 5)
    assert history.evaluate(r, never, now=T0 + timedelta(hours=6)).status == "en attente d'entrée"
    assert history.evaluate(r, never, now=LATER).status == "non déclenché"
    filled = candles([(1995, 2010, 2005), (1988, 2000, 1995), (1995, 2031, 2030)])
    o = history.evaluate(r, filled, now=LATER)
    assert o.status == "gagnant" and o.filled_at == filled.index[1]


def test_expired_trade_is_marked_at_last_close_inside_window():
    r = rec(max_hours=3)
    path = [(1995, 2010, 2005), (2000, 2012, 2010), (2005, 2015, 2010), (2030, 2060, 2050)]
    o = history.evaluate(r, candles(path), now=LATER)
    # La 4e bougie est hors de la fenêtre de 3 heures : elle ne compte pas.
    assert o.status == "expiré"
    assert o.exit_price == 2010
    assert o.r_multiple == pytest.approx(0.5)


def test_wait_decision_and_agent_accuracy():
    up = candles([(2000, 2030, 2025)])
    pairs = [
        (rec(decision="ATTENDRE", signals={"technique": "HAUSSIER", "sentiment": "BAISSIER"}),
         None),
        (rec(signals={"technique": "HAUSSIER", "sentiment": "NEUTRE"}), None),
    ]
    pairs = [(r, history.evaluate(r, up, now=LATER)) for r, _ in pairs]
    assert pairs[0][1].status == "pas de trade"
    s = history.statistics(pairs)
    assert s["justesse_agents"]["technique"] == {"signaux": 2, "justes": 2, "taux": 100.0}
    assert s["justesse_agents"]["sentiment"]["justes"] == 0
    assert s["justesse_agents"]["chef de desk"]["signaux"] == 1


def test_save_and_load_roundtrip(tmp_path):
    con = history.connect(str(tmp_path / "h.db"))
    d = agents.TradingDecision(
        decision="VENTE", confiance=55, horizon="2 jours", synthese="s", prix_entree=100.0,
        zone_entree="z", stop_loss=105.0, take_profits=[95.0, 90.0], vues_agents=[],
        arguments_pour=[], arguments_contre=[], conditions_invalidation=[],
        evenements_a_surveiller=[],
    )
    reports = [agents.AgentReport(key="technique", name="t", icon="", direction="BAISSIER")]
    history.save(con, asset="BTCUSD", style="Intraday (quelques heures)", price=101.0,
                 decision=d, reports=reports, created_at=T0)
    [r] = history.load(con)
    assert (r.decision, r.entry, r.stop, r.targets) == ("VENTE", 100.0, 105.0, [95.0, 90.0])
    assert r.max_hours == 24 and r.created_at == T0
    assert r.signals == {"technique": "BAISSIER"}


# ------------------------------------------------------------------ sources de prix


def test_twelvedata_parsing(monkeypatch):
    payload = {
        "status": "ok",
        "values": [  # Twelve Data renvoie du plus récent au plus ancien
            {"datetime": "2026-03-02 11:00:00", "open": "2", "high": "3", "low": "1", "close": "2.5"},
            {"datetime": "2026-03-02 10:00:00", "open": "1", "high": "2", "low": "0.5", "close": "2"},
        ],
    }
    monkeypatch.setattr(data, "_get_json", lambda url, params=None: payload)
    df = data.twelvedata_ohlcv("XAU/USD", "1h", 2, "key")
    assert list(df.columns) == data.OHLCV
    assert df.index.is_monotonic_increasing and df["Close"].iloc[-1] == 2.5
    assert (df["Volume"] == 0).all()

    monkeypatch.setattr(data, "_get_json", lambda url, params=None: {
        "status": "error", "message": "invalid api key"})
    with pytest.raises(data.MarketDataError, match="invalid api key"):
        data.twelvedata_ohlcv("XAU/USD", "1h", 2, "key")


def _fake_yahoo(monkeypatch, last_close=2010.0):
    h = make_ohlcv(n=200)
    h[["Open", "High", "Low", "Close"]] += last_close - h["Close"].iloc[-1]
    monkeypatch.setattr(data, "fetch_ohlcv", lambda ticker, period="60d", interval="1h",
                        start=None: h if interval == "1h" else data.resample(h, "1D"))
    monkeypatch.setattr(config, "TWELVEDATA_API_KEY", "")


def test_gold_futures_are_shifted_to_spot(monkeypatch):
    _fake_yahoo(monkeypatch, last_close=2010.0)
    monkeypatch.setattr(data, "gold_spot_price", lambda: 2000.0)
    p = data.get_price_history(ASSETS["XAUUSD"])
    assert p.frames["1h"]["Close"].iloc[-1] == pytest.approx(2000.0)
    assert p.frames["4h"]["Close"].iloc[-1] == pytest.approx(2000.0)
    assert "recalé sur le spot" in p.source


def test_gold_not_shifted_when_spot_missing_or_absurd(monkeypatch):
    _fake_yahoo(monkeypatch, last_close=2010.0)
    for spot in (None, 1000.0):
        monkeypatch.setattr(data, "gold_spot_price", lambda: spot)
        p = data.get_price_history(ASSETS["XAUUSD"])
        assert p.frames["1h"]["Close"].iloc[-1] == pytest.approx(2010.0)
        assert "NON recalé" in p.source


def test_twelvedata_failure_falls_back_to_yahoo(monkeypatch):
    _fake_yahoo(monkeypatch)
    monkeypatch.setattr(config, "TWELVEDATA_API_KEY", "key")

    def boom(*a, **k):
        raise data.MarketDataError("quota dépassé")

    monkeypatch.setattr(data, "twelvedata_ohlcv", boom)
    p = data.get_price_history(ASSETS["BTCUSD"])
    assert p.source.startswith("Yahoo Finance BTC-USD")
    assert "quota dépassé" in p.source


def test_sentiment_snapshot_for_btc(monkeypatch):
    monkeypatch.setattr(data, "crypto_fear_greed", lambda: [
        {"date": f"2026-03-{31 - i:02d}", "valeur": 20 + i, "classe": "Fear"} for i in range(31)
    ])
    daily = make_ohlcv(n=300, freq="1D")
    text = data.sentiment_snapshot(ASSETS["BTCUSD"], {"1d": daily})
    assert "aujourd'hui (2026-03-31) : 20" in text
    assert "il y a 30 jours" in text
    assert "range 52 semaines" in text
    assert "Fear & Greed" not in data.sentiment_snapshot(ASSETS["XAUUSD"], {"1d": daily})
