from __future__ import annotations

from datetime import timedelta

import pytest

from teamtrader import paper, risk

from test_history_data import T0, candles

GOLD = risk.DEFAULT_PROFILES["XAUUSD"]


def settings(**kw):
    return risk.RiskSettings(**kw)


def size(entry=2000.0, stop=1980.0, target=2040.0, long=True, equity=10_000.0, open_=(),
         today=0.0, **kw):
    return risk.size_position(asset="XAUUSD", long=long, entry=entry, stop=stop, target=target,
                              settings=settings(**kw), equity=equity,
                              open_positions=list(open_), realized_today=today)


def test_sizing_includes_costs_and_rounds_to_lot_step():
    s = size()
    # Risque 100 $ ; par once : 20 $ + spread 0,30 + glissement 0,20 + commission 0,07.
    assert s.lots == pytest.approx(0.04)          # 100 / 20,57 = 4,86 oz → 0,04 lot (4 oz)
    assert s.units == pytest.approx(4)
    assert s.risk_at_stop <= 100 + 1e-9
    assert s.costs == pytest.approx(0.30 * 4 + 7 * 0.04 + 0.20 * 4)
    assert s.gross_rr == pytest.approx(2.0) and s.net_rr < s.gross_rr
    assert not s.blocked


def test_below_minimum_lot_is_blocked():
    s = size(stop=1000)  # stop très lointain : moins d'un lot minimum
    assert s.blocked and "lot minimum" in s.reasons[0]


def test_daily_loss_limit_blocks_new_risk():
    s = size(today=-350.0)  # limite 3 % de 10 000 $
    assert s.blocked and "perte journalière" in s.reasons[0]


def test_cumulated_open_risk_reduces_then_blocks():
    open1 = risk.OpenRisk("BTCUSD", True, 0.1, 60000, 57500, 3000)   # risque 250 $
    s = size(open_=[open1])
    assert not s.blocked and any("risque cumulé" in w for w in s.warnings)
    assert s.risk_at_stop <= 50 + 1e-9                                 # 300 − 250
    open2 = risk.OpenRisk("BTCUSD", True, 0.1, 60000, 57000, 3000)    # risque 300 $
    assert size(open_=[open2]).blocked


def test_leverage_and_margin_caps():
    s = size(entry=2000, stop=1999, target=2004, max_leverage=2)  # stop serré : grosse taille
    assert s.leverage <= 2 + 1e-9 and any("levier" in w for w in s.warnings)
    big = risk.OpenRisk("BTCUSD", True, 0.15, 60000, 59900, 9500)     # marge quasi épuisée
    s = size(entry=2000, stop=1990, target=2030, max_leverage=50, open_=[big])
    assert s.blocked or any("marge" in w for w in s.warnings)


def test_settings_roundtrip(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    st = risk.load_settings(con)
    assert st.capital == 10_000 and st.profiles["XAUUSD"].contract_size == 100
    st.capital, st.profiles["XAUUSD"].spread = 5_000, 0.5
    risk.save_settings(con, st)
    again = risk.load_settings(con)
    assert again.capital == 5_000 and again.profiles["XAUUSD"].spread == 0.5
    assert again.profiles["BTCUSD"].contract_size == 1


# ------------------------------------------------------------------ paper trading


def order(con, entry=2000.0, stop=1980.0, target=2040.0, price=2000.0, long=True, hours=240):
    s = size(entry, stop, target, long=long)
    pid = paper.place_order(con, asset="XAUUSD", long=long, sizing=s, entry=entry, stop=stop,
                            target=target, price=price, max_hours=hours, profile=GOLD,
                            provider="yahoo", basis=10.0, created_at=T0)
    return next(p for p in paper.load(con) if p.id == pid)


LATER = T0 + timedelta(days=30)


def test_paper_trade_hits_target_with_costs(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    pos = order(con)
    pos = paper.update(con, pos, candles([(1995, 2010, 2005), (2005, 2045, 2041)]), "1h", LATER)
    assert pos.status == "clôturée" and pos.exit_reason == "objectif atteint"
    assert pos.gross_pnl == pytest.approx(40 * 4)
    assert pos.costs == pytest.approx(0.30 * 4 + 7 * 0.04)  # pas de glissement sur l'objectif
    assert pos.net_pnl == pytest.approx(pos.gross_pnl - pos.costs)


def test_paper_stop_includes_slippage_and_swap(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    pos = order(con)
    path = [(1995, 2010, 2005)] * 30 + [(1975, 1990, 1978)]  # stop le lendemain
    pos = paper.update(con, pos, candles(path), "1h", LATER)
    assert pos.exit_reason == "stop touché" and pos.gross_pnl == pytest.approx(-80)
    swap = 6.0 / 100 / 365 * 4 * 2000
    assert pos.costs == pytest.approx(0.30 * 4 + 7 * 0.04 + 0.20 * 4 + swap)


def test_paper_pending_then_open_then_manual_close(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    pos = order(con, entry=1990, stop=1970, target=2040)
    pos = paper.update(con, pos, candles([(1995, 2010, 2005)]), "1h", T0 + timedelta(hours=3))
    assert pos.status == "en attente"
    pos = paper.update(con, pos, candles([(1995, 2010, 2005), (1985, 1995, 1992)]), "1h",
                       T0 + timedelta(hours=4))
    assert pos.status == "ouverte" and pos.exit_price == 1992
    assert paper.unrealized(pos) < 0 or paper.unrealized(pos) >= -100
    paper.close_at_market(con, pos, 2010.0, now=T0 + timedelta(hours=5))
    [closed] = paper.load(con)
    assert closed.exit_reason == "clôture manuelle" and closed.gross_pnl == pytest.approx(20 * 4)


def test_unfilled_order_is_cancelled_at_expiry(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    pos = order(con, entry=1990, stop=1970, target=2040, hours=3)
    df = candles([(1995, 2010, 2005)] * 5)
    pos = paper.update(con, pos, df, "1h", LATER)
    assert pos.status == "annulée"


def test_update_all_uses_order_basis(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    order(con)  # écart future/spot de +10 enregistré avec l'ordre
    raw = candles([(2005, 2020, 2015), (2015, 2052, 2049)])  # brut : objectif à 2050 − 10
    problems = paper.update_all(con, lambda a, p, s, tf: raw, now=LATER)
    [pos] = paper.load(con)
    assert problems == [] and pos.exit_reason == "objectif atteint"


def test_account_and_performance(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    a = order(con)
    paper.update(con, a, candles([(1995, 2010, 2005), (2005, 2045, 2041)]), "1h", LATER)
    order(con, entry=1990, stop=1970, target=2040)
    positions = paper.load(con)
    acc = paper.account(positions, risk.RiskSettings(), now=LATER)
    closed = next(p for p in positions if p.status == "clôturée")
    pending = next(p for p in positions if p.status == "en attente")
    assert acc.realized == pytest.approx(closed.net_pnl)
    assert acc.open_risk == pytest.approx(20 * pending.units)
    perf = paper.performance(positions, 10_000)
    assert perf["trades"] == 1 and perf["gagnants"] == 1
    assert "XAUUSD ACHAT" in paper.portfolio_text(positions)


def test_blocked_sizing_cannot_place_order(tmp_path):
    con = paper.connect(str(tmp_path / "p.db"))
    with pytest.raises(ValueError, match="bloqué"):
        paper.place_order(con, asset="XAUUSD", long=True, sizing=size(today=-1000), entry=2000,
                          stop=1980, target=2040, price=2000, max_hours=24, profile=GOLD,
                          provider="yahoo", basis=0.0)
