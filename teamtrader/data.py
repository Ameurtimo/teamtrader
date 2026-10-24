"""Récupération des données de marché (Yahoo Finance via yfinance)."""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from .config import MACRO_TICKERS, Asset

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


class MarketDataError(RuntimeError):
    pass


def fetch_ohlcv(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise MarketDataError(f"Aucune donnée reçue pour {ticker} ({interval}, {period}).")
    df = df[OHLCV].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Close"])


def get_price_history(asset: Asset) -> dict[str, pd.DataFrame]:
    """Bougies 1h, 4h (rééchantillonnées) et journalières."""
    hourly = fetch_ohlcv(asset.yahoo, period="60d", interval="1h")
    daily = fetch_ohlcv(asset.yahoo, period="2y", interval="1d")
    return {"1h": hourly, "4h": resample(hourly, "4h"), "1d": daily}


def _pct(now: float, before: float) -> float | None:
    if before is None or pd.isna(before) or before == 0:
        return None
    return (now / before - 1.0) * 100.0


def macro_snapshot() -> list[dict]:
    """Dernier cours et variations 1j / 5j / 1 mois pour chaque indicateur macro.

    Un indicateur indisponible est simplement ignoré : l'agent fondamental
    complète avec la recherche web.
    """
    rows = []
    for name, ticker in MACRO_TICKERS.items():
        try:
            close = fetch_ohlcv(ticker, period="3mo", interval="1d")["Close"]
        except Exception:
            continue
        if len(close) < 2:
            continue
        last = float(close.iloc[-1])
        rows.append(
            {
                "nom": name,
                "ticker": ticker,
                "dernier": last,
                "var_1j_%": _pct(last, close.iloc[-2]),
                "var_5j_%": _pct(last, close.iloc[-6]) if len(close) > 5 else None,
                "var_1m_%": _pct(last, close.iloc[-22]) if len(close) > 21 else None,
            }
        )
    return rows


def format_macro(rows: list[dict]) -> str:
    if not rows:
        return "Données macro indisponibles : utilise la recherche web."

    def f(v):
        return "n/d" if v is None else f"{v:+.2f}%"

    lines = ["| Indicateur | Dernier | 1 jour | 5 jours | 1 mois |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['nom']} | {r['dernier']:.2f} | {f(r['var_1j_%'])} | "
            f"{f(r['var_5j_%'])} | {f(r['var_1m_%'])} |"
        )
    return "\n".join(lines)
