from __future__ import annotations

import math
from types import SimpleNamespace as NS

import pytest

from teamtrader import agents, pipeline, validation
from teamtrader.config import ASSETS

from test_teamtrader import FakeClient, good_reports, make_ohlcv


def dec(kind="ACHAT", entry=2000.0, stop=1980.0, targets=(2040.0, 2060.0), conf=70, poids=50):
    return agents.TradingDecision(
        decision=kind, confiance=conf, horizon="", synthese="Plan.", prix_entree=entry,
        zone_entree="", stop_loss=stop, take_profits=list(targets),
        vues_agents=[agents.AgentView(agent="technique", direction="HAUSSIER",
                                      poids_dans_la_decision=poids, commentaire="")],
        arguments_pour=[], arguments_contre=[], conditions_invalidation=[],
        evenements_a_surveiller=[],
    )


def errors(d, price=2000.0):
    return validation.validate_decision(d, price).errors


def test_valid_long_and_short():
    assert errors(dec()) == []
    assert errors(dec("VENTE", 2000, 2020, (1960, 1940))) == []


@pytest.mark.parametrize("d,needle", [
    (dec(conf=120), "confiance"),
    (dec(conf=-5), "confiance"),
    (dec(poids=150), "poids"),
    (dec(stop=None), "stop-loss est obligatoire"),
    (dec(entry=None), "prix d'entrée est obligatoire"),
    (dec(targets=()), "objectif est obligatoire"),
    (dec(stop=2010), "stop (2010"),
    (dec(targets=(1990,)), "objectif 1 (1990"),
    (dec(targets=(2060, 2040)), "ordonnés"),
    (dec(targets=(2020,)), "risque / rendement"),          # R/R = 1,0
    (dec(entry=-1), "positif et fini"),
    (dec(stop=math.nan), "positif et fini"),
    (dec(targets=(math.inf,)), "positif et fini"),
    (dec(entry=2500, stop=2480, targets=(2600,)), "plus de 10 %"),
    (dec("VENTE", 2000, 1980, (1960,)), "au-dessus de l'entrée"),
    (dec("VENTE", 2000, 2020, (2040,)), "sous l'entrée"),
])
def test_invalid_decisions(d, needle):
    assert any(needle in e for e in errors(d)), errors(d)


def test_wait_decision_only_needs_valid_numbers():
    assert errors(dec("ATTENDRE", entry=None, stop=None, targets=())) == []
    # Un plan conditionnel incohérent reste accepté en ATTENDRE, mais pas des prix absurdes.
    assert errors(dec("ATTENDRE", stop=2010)) == []
    assert errors(dec("ATTENDRE", entry=0)) != []


def test_signed_risk_reward():
    assert validation.risk_reward(2000, 1980, 2050, "ACHAT") == pytest.approx(2.5)
    assert validation.risk_reward(2000, 1980, 1990, "ACHAT") == pytest.approx(-0.5)
    assert validation.risk_reward(2000, 2020, 1950, "VENTE") == pytest.approx(2.5)
    assert validation.risk_reward(2000, 2020, 1950) == pytest.approx(2.5)  # sens déduit
    assert validation.risk_reward(2000, 2020, 2050, "ACHAT") is None       # stop au-dessus


def ctx(price=2000.0):
    h = make_ohlcv()
    return pipeline.Context(asset=ASSETS["XAUUSD"], style="swing", frames={"1h": h},
                            price=price, technical="tech", macro="")


def reply(d):
    return NS(stop_reason="end_turn", parsed_output=d, content=[])


def test_invalid_decision_is_sent_back_once_and_corrected():
    client = FakeClient([reply(dec(stop=2010)), reply(dec())])
    result = pipeline.decide(client, ctx(), good_reports())
    assert result.status == "corrigée" and result.decision.decision == "ACHAT"
    assert any("stop" in e for e in result.validation_errors)
    second_prompt = client.calls[1]["messages"][0]["content"]
    assert "REJETÉE" in second_prompt and "doit être sous l'entrée" in second_prompt
    assert len(result.usage) == 2


def test_still_invalid_decision_is_rejected_to_wait_without_levels():
    client = FakeClient([reply(dec(targets=(2020,))), reply(dec(targets=(2010,)))])
    result = pipeline.decide(client, ctx(), good_reports())
    d = result.decision
    assert result.status == "rejetée"
    assert d.decision == "ATTENDRE" and d.prix_entree is None and d.take_profits == []
    assert d.synthese.startswith("❌ Décision ACHAT rejetée")
