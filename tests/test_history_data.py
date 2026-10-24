from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from teamtrader import agents, config, data, history, replay
from teamtrader.config import ASSETS

from test_teamtrader import make_ohlcv

T0 = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)  # lundi 10:00 UTC
LATER = T0 + timedelta(days=30)


def candles(path: list[tuple[float, float, float]], start=None, covered_until=None) -> pd.DataFrame:
    """Bougies 1h à partir de T0 + 1h : liste de (bas, haut, clôture).

    covered_until : ajoute une bougie à cette date pour signaler que les données
    couvrent la période jusque-là."""
    start = start or T0 + timedelta(hours=1)
    idx = list(pd.date_range(start, periods=len(path), freq="1h", tz="UTC"))
    path = list(path)
    if covered_until is not None:
        idx.append(pd.Timestamp(covered_until))
        path.append(path[-1])
    return pd.DataFrame(
        {
            "Open": [c for _, _, c in path], "High": [h for _, h, _ in path],
            "Low": [lo for lo, _, _ in path], "Close": [c for _, _, c in path], "Volume": 0,
        },
        index=pd.DatetimeIndex(idx),
    )


def rec(decision="ACHAT", entry=2000.0, stop=1980.0, targets=(2040.0,), price=2000.0,
        max_hours=240, signals=None, created_at=T0, asset="XAUUSD", confidence=60, rid=1):
    return history.Record(
        id=rid, created_at=created_at, asset=asset, style="Swing (quelques jours)",
        max_hours=max_hours, price=price, decision=decision, confidence=confidence,
        entry=entry, stop=stop, targets=list(targets), horizon="", signals=signals or {},
    )


END = T0 + timedelta(hours=240)


def test_long_hits_target():
    o = history.evaluate(rec(), candles([(1995, 2010, 2005), (2005, 2045, 2041)],
                                        covered_until=END), now=LATER)
    assert o.status == "objectif" and o.window_complete
    assert o.r_multiple == pytest.approx(2.0)


def test_short_hits_stop():
    r = rec(decision="VENTE", stop=2020, targets=(1960,))
    o = history.evaluate(r, candles([(1995, 2010, 2005), (2005, 2025, 2022)]), now=LATER)
    assert o.status == "stop" and o.r_multiple == -1.0


def test_same_candle_touching_stop_and_target_counts_as_loss_and_is_flagged():
    o = history.evaluate(rec(), candles([(1999, 2001, 2000), (1975, 2045, 2000)]), now=LATER)
    assert o.status == "stop"
    assert any("même bougie" in a for a in o.ambiguities)


def test_limit_entry_waits_for_fill_and_needs_coverage_to_expire():
    r = rec(entry=1990, stop=1970, targets=(2030,))
    never = candles([(1995, 2010, 2005)] * 5)
    assert history.evaluate(r, never, now=T0 + timedelta(hours=7)).status == "en attente d'entrée"
    # Échéance passée mais données qui s'arrêtent avant : on ne conclut pas.
    o = history.evaluate(r, never, now=LATER)
    assert o.status == "en attente d'entrée" and not o.window_complete
    assert "pas encore entièrement couverte" in o.data_note
    covered = candles([(1995, 2010, 2005)] * 5, covered_until=END)
    assert history.evaluate(r, covered, now=LATER).status == "non déclenché"
    filled = candles([(1995, 2010, 2005), (1988, 2000, 1995), (1995, 2031, 2030)])
    o = history.evaluate(r, filled, now=LATER)
    assert o.status == "objectif" and o.filled_at == filled.index[1].isoformat()


def test_target_in_fill_candle_is_not_counted():
    r = rec(entry=1990, stop=1970, targets=(2030,), max_hours=3)
    path = [(1995, 2010, 2005), (1988, 2031, 2000), (1995, 2010, 2005)]
    o = history.evaluate(r, candles(path, covered_until=T0 + timedelta(hours=4)), now=LATER)
    assert o.status == "expiré"
    assert any("bougie d'entrée" in a for a in o.ambiguities)


def test_decision_candle_is_included_and_flagged():
    """Décision à 10:15 : la bougie de 10:00 compte (le stop a pu être touché à 10:30)."""
    r = rec(created_at=T0 + timedelta(minutes=15))
    df = candles([(1975, 2001, 1985), (1985, 1995, 1990)], start=T0)
    o = history.evaluate(r, df, now=LATER)
    assert o.status == "stop"
    assert any("bougie de la décision" in a for a in o.ambiguities)


def test_open_candle_is_ignored():
    r = rec()
    df = candles([(1975, 2001, 1985)])  # bougie 11:00-12:00 encore en formation à 11:30
    assert history.evaluate(r, df, now=T0 + timedelta(hours=1, minutes=30)).status == \
        "en attente de données"
    assert history.evaluate(r, df, now=T0 + timedelta(hours=2)).status == "stop"


def test_expired_trade_is_marked_at_last_close_inside_window():
    r = rec(max_hours=3)
    path = [(1995, 2010, 2005), (2000, 2012, 2010), (2005, 2015, 2010), (2030, 2060, 2050)]
    o = history.evaluate(r, candles(path), now=LATER)
    # La 4e bougie (13:00) commence à l'échéance : elle ne compte pas, mais prouve la couverture.
    assert o.status == "expiré" and o.window_complete
    assert o.exit_price == 2010
    assert o.r_multiple == pytest.approx(0.5)


def test_unexpected_gaps_are_reported_but_not_gold_weekend():
    btc = rec(asset="BTCUSD", max_hours=24)
    df = candles([(1995, 2010, 2005)] * 3)
    df = pd.concat([df, candles([(1995, 2010, 2005)] * 3, start=T0 + timedelta(hours=10),
                                covered_until=T0 + timedelta(hours=30))])
    assert "1 trou(s)" in history.evaluate(btc, df, now=LATER).data_note
    friday = datetime(2026, 3, 6, 20, tzinfo=timezone.utc)
    gold = rec(created_at=friday, max_hours=72)
    week_end = pd.concat([candles([(1995, 2010, 2005)], start=friday),
                          candles([(1995, 2010, 2005)] * 30,
                                  start=datetime(2026, 3, 8, 22, tzinfo=timezone.utc))])
    assert "trou" not in history.evaluate(gold, week_end, now=LATER).data_note


def test_wait_decision_and_agent_accuracy():
    up = candles([(2000, 2030, 2025)], covered_until=END)
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


def decision(kind="VENTE", entry=100.0, stop=105.0, targets=(95.0, 90.0), conf=55):
    return agents.TradingDecision(
        decision=kind, confiance=conf, horizon="2 jours", synthese="s", prix_entree=entry,
        zone_entree="z", stop_loss=stop, take_profits=list(targets), vues_agents=[],
        arguments_pour=[], arguments_contre=[], conditions_invalidation=[],
        evenements_a_surveiller=[],
    )


def test_save_and_load_roundtrip(tmp_path):
    con = history.connect(str(tmp_path / "h.db"))
    reports = [agents.AgentReport(key="technique", name="t", icon="", direction="BAISSIER")]
    history.save(con, asset="BTCUSD", style="Intraday (quelques heures)", price=101.0,
                 decision=decision(), reports=reports, created_at=T0, provider="twelvedata",
                 basis=0.0, analysis={"modele": "claude-opus-5-5"})
    [r] = history.load(con)
    assert (r.decision, r.entry, r.stop, r.targets) == ("VENTE", 100.0, 105.0, [95.0, 90.0])
    assert r.max_hours == 24 and r.created_at == T0
    assert r.signals == {"technique": "BAISSIER"}
    assert (r.provider, r.basis, r.analysis) == ("twelvedata", 0.0, {"modele": "claude-opus-5-5"})


def test_results_are_frozen_with_original_basis_and_reproducible(tmp_path, monkeypatch):
    con = history.connect(str(tmp_path / "h.db"))
    # Décision prise sur l'or Yahoo recalé avec un écart future/spot de +10.
    history.save(con, asset="XAUUSD", style="Intraday (quelques heures)", price=2000.0,
                 decision=decision("ACHAT", 2000.0, 1980.0, (2040.0,), conf=70),
                 reports=[], created_at=T0, provider="yahoo", basis=10.0)
    raw = candles([(2005, 2020, 2015), (2015, 2052, 2049)],
                  covered_until=T0 + timedelta(hours=25))
    calls = []

    def fetch(asset, provider, start, tf):
        calls.append((asset, provider, tf))
        if tf == "15m":
            raise data.MarketDataError("pas de 15 min")
        return raw

    [(r, o)] = history.evaluate_all(con, history.load(con), fetch, now=LATER)
    # Brut 2052 − écart 10 = 2042 ≥ objectif 2040 : gagnant, avec l'écart d'origine.
    assert o.status == "objectif" and o.frozen
    assert calls == [("XAUUSD", "yahoo", "15m"), ("XAUUSD", "yahoo", "1h")]

    def broken(*a):
        raise AssertionError("ne doit plus être appelé : résultat figé")

    [(r2, o2)] = history.evaluate_all(con, history.load(con), broken, now=LATER)
    assert (o2.status, o2.r_multiple) == (o.status, o.r_multiple)

    # Nouvelle version de méthode : recalcul à partir des bougies conservées, sans réseau.
    monkeypatch.setattr(replay, "METHOD_VERSION", "v3")
    [(r3, o3)] = history.evaluate_all(con, history.load(con), broken, now=LATER)
    assert o3.status == "objectif" and o3.method == "v3"
    assert "bougies conservées" in o3.data_note


def test_legacy_decision_without_basis_uses_current_basis_and_says_so(tmp_path):
    con = history.connect(str(tmp_path / "h.db"))
    history.save(con, asset="XAUUSD", style="Intraday (quelques heures)", price=2000.0,
                 decision=decision("ACHAT", 2000.0, 1980.0, (2040.0,)), reports=[],
                 created_at=T0)
    raw = candles([(2005, 2020, 2015)], covered_until=T0 + timedelta(hours=25))
    [(r, o)] = history.evaluate_all(con, history.load(con), lambda *a: raw,
                                    legacy_basis=lambda asset: 15.0, now=LATER)
    assert "estimé avec l'écart actuel" in o.data_note


def test_statistics_separate_targets_stops_and_expirations():
    def o(status, r, closed):
        return history.Outcome(status, r_multiple=r, closed_at=closed, window_complete=True,
                               price_move_pct=1.0)
    pairs = [
        (rec(confidence=85, rid=1), o("objectif", 2.0, "2026-03-03")),
        (rec(confidence=85, rid=2), o("stop", -1.0, "2026-03-04")),
        (rec(confidence=55, rid=3), o("stop", -1.0, "2026-03-05")),
        (rec(confidence=55, rid=4, asset="BTCUSD"), o("expiré", 0.5, "2026-03-06")),
        (rec(confidence=55, rid=5, asset="BTCUSD"), o("expiré", -0.3, "2026-03-07")),
        (rec(rid=6), history.Outcome("en cours", r_multiple=0.2)),
    ]
    s = history.statistics(pairs)
    assert (s["objectifs"], s["stops"], s["expires_gagnants"], s["expires_perdants"]) == (1, 2, 1, 1)
    assert s["taux_objectif"] == pytest.approx(100 / 3)
    assert s["taux_gagnants"] == pytest.approx(40.0)
    assert s["r_total"] == pytest.approx(0.2)
    assert s["profit_factor"] == pytest.approx(2.5 / 2.3)
    assert s["drawdown_max_r"] == pytest.approx(-2.0)   # 2 → 1 → 0
    assert s["en_cours"] == 1
    assert s["par_actif"]["BTCUSD"]["trades_clotures"] == 2
    assert s["calibration"]["≥ 80 %"]["taux_gagnants"] == 50.0


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


# ------------------------------------------------------------------ couverture du début de période


def test_data_starting_after_decision_is_not_evaluated():
    late = candles([(1975, 2045, 2000)] * 3, start=T0 + timedelta(hours=6), covered_until=END)
    o = history.evaluate(rec(asset="BTCUSD"), late, now=LATER)
    assert o.status == "en attente de données" and not o.window_complete
    assert "début de la période" in o.data_note


def test_gold_decision_before_weekend_closure_is_covered():
    friday = datetime(2026, 3, 6, 20, 30, tzinfo=timezone.utc)
    sunday = datetime(2026, 3, 8, 22, tzinfo=timezone.utc)
    df = candles([(1995, 2045, 2041)] * 3, start=sunday)
    o = history.evaluate(rec(created_at=friday, max_hours=72), df, now=LATER)
    assert o.status == "objectif" and "début" not in o.data_note


def test_decision_at_candle_open_is_not_ambiguous():
    df = candles([(1975, 2001, 1985)], start=T0)
    o = history.evaluate(rec(), df, now=LATER)
    assert o.status == "stop" and not any("bougie de la décision" in a for a in o.ambiguities)


def test_fetch_best_falls_back_to_1h_when_15m_does_not_reach_start():
    start = T0
    m15 = candles([(1, 2, 1.5)] * 4, start=T0 + timedelta(days=3))
    h1 = candles([(1, 2, 1.5)] * 4, start=T0)
    df, tf, note = replay.fetch_best(lambda a, p, s, tf: m15 if tf == "15m" else h1,
                                     "BTCUSD", "yahoo", start)
    assert tf == "1h" and df is h1
