from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from teamtrader import agents, costs, data, pipeline, quality
from teamtrader.config import ASSETS

from test_teamtrader import FakeClient, good_reports, make_ohlcv

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # mercredi


def hourly(last_start):
    idx = pd.date_range(end=last_start, periods=10, freq="1h", tz="UTC")
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}, index=idx)


# ------------------------------------------------------------------ fraîcheur


def test_price_freshness():
    fresh = quality.price_item("BTCUSD", hourly(NOW - timedelta(hours=1)), "src", NOW)
    assert fresh.status == quality.OK and fresh.essential
    stale = quality.price_item("BTCUSD", hourly(NOW - timedelta(hours=6)), "src", NOW)
    assert stale.status == quality.STALE
    assert quality.price_item("BTCUSD", None, "src", NOW).status == quality.MISSING


def test_gold_weekend_is_not_stale():
    saturday = datetime(2026, 3, 7, 12, tzinfo=timezone.utc)
    friday_close = datetime(2026, 3, 6, 20, tzinfo=timezone.utc)
    item = quality.price_item("XAUUSD", hourly(friday_close), "src", saturday)
    assert item.status == quality.OK and item.detail == "marché fermé"
    # Le même retard un mercredi est anormal.
    assert quality.price_item("XAUUSD", hourly(NOW - timedelta(hours=16)), "src",
                              NOW).status == quality.STALE


def test_macro_item_counts_indicators():
    rows = [{"date": NOW - timedelta(days=1)}] * 9
    assert data.macro_item(rows, NOW).status == quality.OK
    assert data.macro_item(rows[:3], NOW).status == quality.MISSING
    old = [{"date": NOW - timedelta(days=9)}] * 9
    assert data.macro_item(old, NOW).status == quality.STALE


# ------------------------------------------------------------------ seuil de qualité


def items(price_status=quality.OK):
    return [quality.DataItem("Prix", "src", price_status, essential=True),
            quality.DataItem("COT or", "CFTC", quality.MISSING)]


def test_quality_gate():
    q = quality.assess(items(), good_reports(), smc_mode=False)
    assert q.ok and any("COT" in w for w in q.warnings) and 0 < q.score < 100
    assert not quality.assess(items(quality.STALE), good_reports(), False).ok
    no_tech = good_reports(("fondamental", "geopolitique", "sentiment"))
    assert any("technique" in b for b in quality.assess(items(), no_tech, False).blocking)
    two = good_reports(("technique", "sentiment"))
    assert any("Seulement 2" in b for b in quality.assess(items(), two, False).blocking)
    assert not quality.assess(items(), good_reports(), smc_mode=True).ok  # rapport SMC manquant
    failed = good_reports()
    failed[0].error = "Erreur API"
    assert quality.assess(items(), failed, False).ok  # 3 rapports sur 4 suffisent


def test_failed_gate_skips_the_desk_lead_call():
    h = make_ohlcv()
    ctx = pipeline.Context(asset=ASSETS["XAUUSD"], style="swing", frames={"1h": h},
                           price=2000.0, technical="", macro="", data_items=items())
    client = FakeClient([])  # aucun appel autorisé
    result = pipeline.decide(client, ctx, good_reports(("technique",)))
    assert result.status == "bloquée" and result.decision.decision == "ATTENDRE"
    assert client.calls == [] and result.decision.synthese.startswith("⛔")


def test_analysis_record_keeps_reports_parameters_and_costs():
    h = make_ohlcv()
    ctx = pipeline.Context(asset=ASSETS["BTCUSD"], style="swing", frames={"1h": h}, price=1.0,
                           technical="", macro="", data_items=items(), provider="twelvedata")
    reports = good_reports()
    reports[0].usage = [{"label": "fondamental", "model": "claude-opus-5-5", "input_tokens": 10,
                         "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0,
                         "web_searches": 2, "cost_usd": 0.02}]
    result = pipeline.DecisionResult(pipeline.wait_decision(["x"]), quality=quality.assess(
        items(), reports, False))
    rec = pipeline.analysis_record(ctx, reports, result, duration_s=42.0)
    assert rec["parametres"]["modele_demande"] and rec["source_prix"]["fournisseur"] == "twelvedata"
    assert len(rec["rapports"]) == 4 and rec["rapports"][0]["texte"] == "Analyse."
    assert rec["couts"]["web_searches"] == 2 and rec["duree_s"] == 42.0
    assert rec["qualite"]["items"][1]["status"] == quality.MISSING


# ------------------------------------------------------------------ sources structurées


def test_okx_derivatives(monkeypatch):
    def fake(url, params=None):
        if "funding" in url:
            return {"code": "0", "data": [{"fundingRate": "0.0001"}]}
        return {"code": "0", "data": [{"oiCcy": "25000", "ts": "1772600000000"}]}
    monkeypatch.setattr(data, "_get_json", fake)
    d = data.crypto_derivatives()
    assert d["funding_pct"] == pytest.approx(0.01) and d["open_interest_btc"] == 25000
    assert d["source"].startswith("OKX")


def test_derivatives_fall_back_to_binance(monkeypatch):
    def fake(url, params=None):
        if "okx" in url:
            raise ConnectionError("bloqué")
        if "premiumIndex" in url:
            return {"lastFundingRate": "-0.0002"}
        return {"openInterest": "80000", "time": 1772600000000}
    monkeypatch.setattr(data, "_get_json", fake)
    d = data.crypto_derivatives()
    assert d["funding_pct"] == pytest.approx(-0.02) and d["source"].startswith("Binance")


def test_gold_cot(monkeypatch):
    rows = [{"report_date_as_yyyy_mm_dd": f"2026-03-{3 - i:02d}T00:00:00.000" if i < 3
             else f"2026-02-{24 - 7 * (i - 3):02d}T00:00:00.000",
             "noncomm_positions_long_all": str(300000 - 1000 * i),
             "noncomm_positions_short_all": "100000", "open_interest_all": "500000"}
            for i in range(5)]
    monkeypatch.setattr(data, "_get_json", lambda url, params=None: rows)
    c = data.gold_cot()
    assert c["net"] == 200000 and c["net_change_1w"] == 1000 and c["net_change_4w"] == 4000
    text, its = data.sentiment_data(ASSETS["XAUUSD"], {}, now=datetime(2026, 3, 6, tzinfo=timezone.utc))
    assert "+200,000 contrats" in text and its[0].status == quality.OK


def test_sentiment_items_report_missing_sources():
    # Réseau bloqué (conftest) : toutes les sources sont signalées indisponibles.
    text, its = data.sentiment_data(ASSETS["BTCUSD"], {})
    assert {i.name: i.status for i in its} == {
        "Fear & Greed crypto": quality.MISSING, "Funding / open interest BTC": quality.MISSING}
    assert "indisponible" in text


# ------------------------------------------------------------------ coûts IA


def test_usage_cost_estimate():
    resp = NS(model="claude-opus-5-5", usage=NS(
        input_tokens=100_000, output_tokens=10_000, cache_read_input_tokens=50_000,
        cache_creation_input_tokens=0, server_tool_use=NS(web_search_requests=3)))
    u = costs.usage_of(resp, "test")
    assert u["cost_usd"] == pytest.approx(0.4 + 0.2 + 0.01 + 0.03)
    assert costs.usage_of(NS(), "vide")["cost_usd"] == 0


def test_monthly_budget(monkeypatch):
    con = sqlite3.connect(":memory:")
    u = {"label": "x", "model": "m", "input_tokens": 0, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "web_searches": 0, "cost_usd": 1.5}
    costs.record(con, "analyse", [u, u])
    assert costs.month_spend(con) == pytest.approx(3.0)
    monkeypatch.setattr(costs, "MONTHLY_BUDGET_USD", 5.0)
    assert costs.budget_status(con) == (True, pytest.approx(3.0))
    monkeypatch.setattr(costs, "MONTHLY_BUDGET_USD", 2.0)
    assert costs.budget_status(con)[0] is False
    assert costs.summary_by_kind(con)[0][1:] == ("analyse", 2, 0, pytest.approx(3.0))


def test_specialist_records_usage_and_duration():
    resp = NS(stop_reason="end_turn", model="claude-opus-5-5",
              content=[NS(type="text", text="ok\n## Signal\n- Direction : NEUTRE\n- Confiance : 50%")],
              usage=NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0, server_tool_use=None))
    r = agents.run_specialist(FakeClient([resp]), agents.TECHNICAL, "b")
    assert r.ok and r.model == "claude-opus-5-5" and r.usage[0]["cost_usd"] > 0
    assert r.duration_s >= 0
