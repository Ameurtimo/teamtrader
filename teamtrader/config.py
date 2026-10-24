"""Configuration : actifs suivis, indicateurs macro, modèle Claude."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    key: str
    name: str
    yahoo: str
    description: str


ASSETS: dict[str, Asset] = {
    "XAUUSD": Asset(
        key="XAUUSD",
        name="Or (XAU/USD)",
        # Le spot XAU/USD n'est pas fiable sur Yahoo : on utilise le future COMEX,
        # qui suit le spot à quelques dollars près (prime de portage).
        yahoo="GC=F",
        description=(
            "Or physique coté en dollars. Valeur refuge, sensible au dollar (DXY), "
            "aux taux réels US, à la politique de la Fed, aux achats des banques "
            "centrales, aux flux ETF et aux tensions géopolitiques."
        ),
    ),
    "BTCUSD": Asset(
        key="BTCUSD",
        name="Bitcoin (BTC/USD)",
        yahoo="BTC-USD",
        description=(
            "Bitcoin coté en dollars, marché ouvert 24h/24 et 7j/7. Sensible à la "
            "liquidité mondiale, à l'appétit pour le risque (Nasdaq), aux flux des ETF "
            "spot, à la régulation, au cycle de halving et aux données on-chain."
        ),
    ),
}

# Contexte inter-marchés fourni à l'agent fondamental.
MACRO_TICKERS: dict[str, str] = {
    "Indice dollar (DXY)": "DX-Y.NYB",
    "Taux US 10 ans (%)": "^TNX",
    "Taux US 13 semaines (%)": "^IRX",
    "VIX (volatilité actions)": "^VIX",
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Pétrole WTI": "CL=F",
    "Argent (XAG)": "SI=F",
    "Or (XAU)": "GC=F",
    "Bitcoin": "BTC-USD",
    "Ethereum": "ETH-USD",
}

TRADING_STYLES: dict[str, str] = {
    "Intraday (quelques heures)": "intraday, position clôturée dans la journée",
    "Swing (quelques jours)": "swing trading, position tenue de 2 à 10 jours",
    "Position (quelques semaines)": "trading de position, tenue de 2 à 8 semaines",
}

MODEL = os.environ.get("TEAMTRADER_MODEL", "claude-opus-5-5")

# Effort de réflexion : les spécialistes travaillent vite, le décideur réfléchit plus.
SPECIALIST_EFFORT = os.environ.get("TEAMTRADER_SPECIALIST_EFFORT", "medium")
DECISION_EFFORT = os.environ.get("TEAMTRADER_DECISION_EFFORT", "high")

# Nombre maximum de recherches web par agent.
WEB_SEARCH_MAX_USES = int(os.environ.get("TEAMTRADER_WEB_SEARCH_MAX_USES", "6"))
