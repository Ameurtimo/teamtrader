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
    # Symbole spot chez Twelve Data (utilisé si TWELVEDATA_API_KEY est défini).
    twelvedata: str = ""
    # Yahoo ne cote que le future pour l'or : on le recale sur le spot.
    spot_adjust: bool = False


ASSETS: dict[str, Asset] = {
    "XAUUSD": Asset(
        key="XAUUSD",
        name="Or (XAU/USD)",
        # Sans clé Twelve Data, on part du future COMEX (Yahoo) recalé sur le spot.
        yahoo="GC=F",
        description=(
            "Or physique coté en dollars. Valeur refuge, sensible au dollar (DXY), "
            "aux taux réels US, à la politique de la Fed, aux achats des banques "
            "centrales, aux flux ETF et aux tensions géopolitiques."
        ),
        twelvedata="XAU/USD",
        spot_adjust=True,
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
        twelvedata="BTC/USD",
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

# Durée de vie maximale d'une décision (en heures) pour l'évaluer dans l'historique.
STYLE_MAX_HOURS: dict[str, int] = {
    "Intraday (quelques heures)": 24,
    "Swing (quelques jours)": 10 * 24,
    "Position (quelques semaines)": 8 * 7 * 24,
}

MODEL = os.environ.get("TEAMTRADER_MODEL", "claude-opus-5-5")

# Effort de réflexion : les spécialistes travaillent vite, le décideur réfléchit plus.
SPECIALIST_EFFORT = os.environ.get("TEAMTRADER_SPECIALIST_EFFORT", "medium")
DECISION_EFFORT = os.environ.get("TEAMTRADER_DECISION_EFFORT", "high")

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

# Journal des décisions (SQLite).
HISTORY_DB = os.environ.get("TEAMTRADER_DB", "teamtrader_history.db")

# Nombre maximum de recherches web par agent.
WEB_SEARCH_MAX_USES = int(os.environ.get("TEAMTRADER_WEB_SEARCH_MAX_USES", "6"))
