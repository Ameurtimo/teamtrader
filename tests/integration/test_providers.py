"""Vérifications des fournisseurs réels (réseau requis) : pytest -m integration

Ces tests contrôlent que les formats de réponse attendus par TeamTrader sont toujours
ceux des fournisseurs. Ils sont séparés des tests unitaires, qui restent hors ligne.
Les tests qui appellent Claude coûtent quelques centimes : ils ne tournent que si
TEAMTRADER_TEST_CLAUDE=1 et ANTHROPIC_API_KEY sont définis.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from teamtrader import agents, config, data, economic_calendar
from teamtrader.config import ASSETS

pytestmark = pytest.mark.integration
NOW = datetime.now(timezone.utc)


def test_yahoo_gold_and_bitcoin_candles():
    for asset in ASSETS.values():
        df = data.fetch_ohlcv(asset.yahoo, period="5d", interval="1h")
        assert list(df.columns) == data.OHLCV and len(df) > 10
        assert NOW - df.index[-1].to_pydatetime() < timedelta(days=4)
    df15 = data.fetch_ohlcv("BTC-USD", interval="15m", start=NOW - timedelta(days=2))
    assert len(df15) > 50


def test_gold_spot_price():
    spot = data.gold_spot_price()
    assert spot is not None and 500 < spot < 50_000


@pytest.mark.skipif(not config.TWELVEDATA_API_KEY, reason="TWELVEDATA_API_KEY absent")
def test_twelvedata_spot():
    df = data.twelvedata_ohlcv("XAU/USD", "1h", 50, config.TWELVEDATA_API_KEY)
    assert len(df) > 10 and df.index.is_monotonic_increasing


def test_forex_factory_calendar_format():
    raw = data._get_json(economic_calendar.CALENDAR_URL)
    assert isinstance(raw, list) and raw
    assert {"title", "country", "date", "impact"} <= set(raw[0])
    events = economic_calendar.parse_events(raw)
    assert all(e.time.tzinfo is not None for e in events)


def test_crypto_fear_greed():
    fg = data.crypto_fear_greed()
    assert 0 <= fg[0]["valeur"] <= 100


def test_crypto_derivatives_okx_or_binance():
    d = data.crypto_derivatives()
    assert -1 < d["funding_pct"] < 1 and NOW - d["as_of"] < timedelta(days=1)


def test_cftc_gold_cot():
    c = data.gold_cot()
    assert NOW - c["date"] < timedelta(days=21) and c["long"] > 0 and c["short"] > 0


claude = pytest.mark.skipif(
    not (os.environ.get("TEAMTRADER_TEST_CLAUDE") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
    reason="TEAMTRADER_TEST_CLAUDE=1 et ANTHROPIC_API_KEY requis (appel payant)",
)


@claude
def test_claude_specialist_with_web_search():
    import anthropic
    r = agents.run_specialist(anthropic.Anthropic(), agents.GEOPOLITICAL,
                              "Actif : Or (XAU/USD). Rédige une analyse très courte (100 mots).")
    assert r.ok, r.error
    assert r.usage and r.usage[0]["model"]


@claude
def test_claude_structured_decision():
    import anthropic
    d, usage = agents.run_decision(anthropic.Anthropic(), "Actif : Or. Prix 2000. Aucun rapport "
                                   "disponible : décide ATTENDRE.")
    assert d.decision in ("ACHAT", "VENTE", "ATTENDRE") and usage["output_tokens"] > 0
