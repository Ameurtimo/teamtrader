from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

from teamtrader import agents, data, indicators, pipeline
from teamtrader.config import ASSETS


def make_ohlcv(n: int = 400, start: float = 2000.0, drift: float = 0.5, freq: str = "1h"):
    rng = np.random.default_rng(42)
    close = start + np.cumsum(rng.normal(drift, 5, n))
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "Open": close - rng.normal(0, 1, n),
            "High": close + np.abs(rng.normal(3, 1, n)),
            "Low": close - np.abs(rng.normal(3, 1, n)),
            "Close": close,
            "Volume": rng.integers(100, 1000, n),
        },
        index=idx,
    )


# ------------------------------------------------------------------ indicateurs


def test_rsi_bounds_and_extremes():
    df = make_ohlcv()
    r = indicators.rsi(df["Close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    up_only = pd.Series(np.arange(1, 60, dtype=float))
    assert indicators.rsi(up_only).iloc[-1] == 100.0


def test_summary_uptrend_levels_are_consistent():
    df = make_ohlcv(drift=2.0)
    s = indicators.summarize(df)
    assert s["tendance"] == "haussière"
    assert s["atr14"] > 0
    assert all(v < s["prix"] for v in s["supports"])
    assert all(v > s["prix"] for v in s["resistances"])
    assert s["bb_basse"] < s["bb_milieu"] < s["bb_haute"]


def test_resample_to_4h():
    df = make_ohlcv(n=48)
    out = data.resample(df, "4h")
    assert len(out) == 12
    first = df.iloc[:4]
    assert out["High"].iloc[0] == first["High"].max()
    assert out["Close"].iloc[0] == first["Close"].iloc[-1]


def test_format_technical_mentions_each_timeframe():
    h = make_ohlcv()
    text = indicators.format_technical({"1h": h, "4h": data.resample(h, "4h")})
    assert "Unité de temps 1h" in text and "Unité de temps 4h" in text


# ------------------------------------------------------------------ signaux et risque


@pytest.mark.parametrize(
    "text,expected",
    [
        ("## Signal\n- Direction : HAUSSIER\n- Confiance : 72%", ("HAUSSIER", 72)),
        ("- **Direction :** baissier\n- **Confiance :** 55 %", ("BAISSIER", 55)),
        ("pas de section signal", (None, None)),
    ],
)
def test_parse_signal(text, expected):
    assert agents.parse_signal(text) == expected


def test_risk_reward_alias():
    assert pipeline.risk_reward(2000, 1980, 2050) == pytest.approx(2.5)


# ------------------------------------------------------------------ agents (client simulé)


class FakeClient:
    """Imite client.beta.messages.stream(...) en renvoyant des réponses préparées."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        msg = self.responses.pop(0)
        yield NS(get_final_message=lambda: msg)


def good_reports(keys=("fondamental", "technique", "geopolitique", "sentiment")):
    """Rapports exploitables (texte + signal) pour passer le contrôle de qualité."""
    return [agents.AgentReport(key=k, name=k, icon="", text="Analyse.", direction="HAUSSIER",
                               confidence=60) for k in keys]


def text(t):
    return NS(type="text", text=t)


def search_result(url):
    return NS(
        type="web_search_tool_result",
        content=[NS(type="web_search_result", title="Article", url=url)],
    )


def test_specialist_resumes_after_pause_turn_and_collects_sources():
    client = FakeClient(
        [
            NS(stop_reason="pause_turn", content=[search_result("https://a"), text("Début. ")]),
            NS(
                stop_reason="end_turn",
                content=[
                    search_result("https://a"),
                    text("Fin.\n## Signal\n- Direction : BAISSIER\n- Confiance : 60%"),
                ],
            ),
        ]
    )
    r = agents.run_specialist(client, agents.GEOPOLITICAL, "briefing")
    assert r.error is None
    assert r.text.startswith("Début. Fin.")
    assert (r.direction, r.confidence) == ("BAISSIER", 60)
    assert r.sources == [{"title": "Article", "url": "https://a"}]
    # La reprise renvoie la question initiale + la réponse partielle, sans nouveau message.
    second = client.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]
    assert client.calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert client.calls[0]["fallbacks"] == "default"


def test_technical_agent_has_no_web_search_and_refusal_is_reported():
    client = FakeClient([NS(stop_reason="refusal", content=[])])
    r = agents.run_specialist(client, agents.TECHNICAL, "briefing")
    assert r.error
    assert client.calls[0]["tools"] is agents.anthropic.omit


def test_decision_prompt_contains_every_report():
    reports = [
        agents.AgentReport(key=s.key, name=s.name, icon=s.icon, text=f"rapport {s.key}")
        for s in agents.SPECIALISTS
    ]
    reports[2].error = "timeout"
    prompt = agents.build_decision_prompt("Or", 2000.0, "swing", reports, notes="déjà long")
    for s in agents.SPECIALISTS[:2]:
        assert f"rapport {s.key}" in prompt
    assert "rapport indisponible : timeout" in prompt
    assert "déjà long" in prompt


def test_decide_returns_parsed_output():
    decision = agents.TradingDecision(
        decision="ACHAT", confiance=65, horizon="3-5 jours", synthese="ok",
        prix_entree=2000, zone_entree="2000-2005", stop_loss=1980, take_profits=[2050],
        vues_agents=[], arguments_pour=[], arguments_contre=[],
        conditions_invalidation=[], evenements_a_surveiller=[],
    )
    client = FakeClient([NS(stop_reason="end_turn", parsed_output=decision, content=[])])
    h = make_ohlcv()
    ctx = pipeline.Context(
        asset=ASSETS["XAUUSD"], style="swing", frames={"1h": h, "4h": data.resample(h, "4h")},
        price=float(h["Close"].iloc[-1]), technical="tech", macro="macro",
    )
    result = pipeline.decide(client, ctx, good_reports())
    assert result.decision is decision and result.status == "valide"
    assert client.calls[0]["output_format"] is agents.TradingDecision


def test_briefings_are_specialised():
    h = make_ohlcv()
    ctx = pipeline.Context(
        asset=ASSETS["BTCUSD"], style="swing", frames={"1h": h, "4h": data.resample(h, "4h")},
        price=100.0, technical="INDICATEURS", macro="TABLEAU_MACRO", sentiment="SENTI",
    )
    assert [s.key for s in agents.SPECIALISTS] == [
        "fondamental", "technique", "geopolitique", "sentiment"]
    assert "SENTI" in ctx.briefing("sentiment")
    assert "INDICATEURS" in ctx.briefing("technique")
    assert "TABLEAU_MACRO" in ctx.briefing("fondamental")
    geo = ctx.briefing("geopolitique")
    assert "INDICATEURS" not in geo and "TABLEAU_MACRO" not in geo
