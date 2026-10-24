"""Scénario complet de l'interface Streamlit, avec données et modèle simulés."""

from __future__ import annotations

import os

import pytest
from streamlit.testing.v1 import AppTest

from teamtrader import agents, config, costs, data, economic_calendar, pipeline

HARNESS = os.path.join(os.path.dirname(__file__), "app_harness.py")

# Attributs remplacés par le harnais : restaurés après chaque test pour ne pas
# contaminer les autres tests du même processus.
PATCHED = {
    data: ["get_price_history", "get_candles", "current_basis", "macro_snapshot",
           "crypto_fear_greed", "crypto_derivatives", "gold_cot"],
    economic_calendar: ["fetch_us_events"],
    pipeline: ["run_specialists", "decide"],
    agents: ["forecast_calendar"],
    config: ["HISTORY_DB"],
    costs: ["MONTHLY_BUDGET_USD"],
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    for module, names in PATCHED.items():
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(pipeline, "_original_decide", pipeline.decide, raising=False)
    monkeypatch.setenv("TEAMTRADER_TEST_DB", str(tmp_path / "app.db"))
    at = AppTest.from_file(HARNESS, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def ok(at):
    assert not at.exception, [e.value for e in at.exception]


def test_tabs_and_history_load(app):
    assert [t.label for t in app.tabs] == [
        "🔎 Analyse", "📅 Calendrier US", "🎯 Scanner SMC", "💼 Paper trading",
        "📒 Historique et performance"]
    labels = [m.label for m in app.metric]
    assert "Objectif avant stop" in labels and "Fonds propres" in labels


def test_standard_analysis_is_corrected_sized_and_sent_to_paper(app):
    app.sidebar.button[0].click().run()
    ok(app)
    assert any("corrigée" in i.value for i in app.info)  # 1re proposition : R/R trop faible
    sizing = {m.label: m.value for m in app.metric}
    assert sizing["Taille"].endswith("lot(s)") and "R/R net de coûts" in sizing
    [paper_button] = [b for b in app.button if "paper trading" in b.label]
    paper_button.click().run()
    ok(app)
    assert any("Ordre fictif enregistré" in s.value for s in app.success)


def test_smc_analysis_blocks_entry_without_inducement(app):
    app.sidebar.toggle[0].set_value(True).run()
    app.sidebar.button[0].click().run()
    ok(app)  # régression : graphique SMC présent deux fois (analyse + scanner)
    assert any("règle SMC" in w.value for w in app.warning)
    card = next(m.value for m in app.markdown if "Décision du chef" in m.value)
    assert ">ATTENDRE ·" in card


def test_calendar_forecast(app):
    [button] = [b for b in app.button if "Prédire" in b.label]
    button.click().run()
    ok(app)
    assert any("Non-Farm" in e.label for e in app.expander)
