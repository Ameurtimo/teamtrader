"""Récupération des données de marché.

Sources de prix, par ordre de préférence :
1. Twelve Data (spot XAU/USD et BTC/USD) si `TWELVEDATA_API_KEY` est défini ;
2. Yahoo Finance (yfinance). Pour l'or, Yahoo ne cote que le future COMEX `GC=F` :
   on le recale sur le spot en soustrayant l'écart future/spot du moment
   (prix spot fourni par gold-api.com).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

from . import config
from .config import MACRO_TICKERS, Asset

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
HTTP_TIMEOUT = 15
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
GOLD_SPOT_URL = "https://api.gold-api.com/price/XAU"
CRYPTO_FEAR_GREED_URL = "https://api.alternative.me/fng/"
# Au-delà de cet écart future/spot, on soupçonne une donnée aberrante et on ne recale pas.
MAX_BASIS_PCT = 3.0


class MarketDataError(RuntimeError):
    pass


@dataclass
class PriceData:
    frames: dict[str, pd.DataFrame]
    source: str


def _get_json(url: str, params: Optional[dict] = None):
    r = requests.get(
        url, params=params, timeout=HTTP_TIMEOUT, headers={"User-Agent": "TeamTrader/1.0"}
    )
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------ Yahoo Finance


def fetch_ohlcv(
    ticker: str, period: str = "60d", interval: str = "1h", start: Optional[datetime] = None
) -> pd.DataFrame:
    t = yf.Ticker(ticker)
    if start is not None:
        df = t.history(start=start, interval=interval, auto_adjust=False)
    else:
        df = t.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise MarketDataError(f"Aucune donnée reçue pour {ticker} ({interval}).")
    df = df[OHLCV].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index, utc=True)
    return df


# ------------------------------------------------------------------ Twelve Data


def twelvedata_ohlcv(symbol: str, interval: str, outputsize: int, api_key: str) -> pd.DataFrame:
    js = _get_json(
        TWELVEDATA_URL,
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": api_key,
        },
    )
    if not isinstance(js, dict) or js.get("status") == "error" or not js.get("values"):
        msg = js.get("message", "réponse vide") if isinstance(js, dict) else "réponse invalide"
        raise MarketDataError(f"Twelve Data ({symbol} {interval}) : {msg}")
    df = pd.DataFrame(js["values"])
    df.index = pd.to_datetime(df.pop("datetime"), utc=True)
    df = df.rename(columns=str.capitalize)
    if "Volume" not in df:
        df["Volume"] = 0  # le forex et l'or n'ont pas de volume centralisé
    df = df[OHLCV].apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"])
    return df.sort_index()


# ------------------------------------------------------------------ spot de l'or


def gold_spot_price() -> Optional[float]:
    try:
        price = float(_get_json(GOLD_SPOT_URL)["price"])
    except Exception:
        return None
    return price if price > 0 else None


def adjust_to_spot(frames: dict[str, pd.DataFrame], spot: float) -> tuple[dict, float]:
    """Décale toutes les bougies de l'écart future/spot actuel."""
    basis = float(frames["1h"]["Close"].iloc[-1]) - spot
    out = {}
    for tf, df in frames.items():
        df = df.copy()
        df[["Open", "High", "Low", "Close"]] -= basis
        out[tf] = df
    return out, basis


# ------------------------------------------------------------------ point d'entrée


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = df.resample(rule, label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Close"])


def _yahoo_frames(asset: Asset, start: Optional[datetime], with_15m: bool) -> dict:
    frames = {"1h": fetch_ohlcv(asset.yahoo, period="60d", interval="1h", start=start)}
    if start is None:
        frames["1d"] = fetch_ohlcv(asset.yahoo, period="2y", interval="1d")
        if with_15m:
            frames["15m"] = fetch_ohlcv(asset.yahoo, period="30d", interval="15m")
    return frames


def _twelvedata_frames(asset: Asset, start: Optional[datetime], with_15m: bool) -> dict:
    key = config.TWELVEDATA_API_KEY
    if start is None:
        hours = 60 * 24
    else:
        hours = int((datetime.now(timezone.utc) - start) / timedelta(hours=1)) + 24
    frames = {"1h": twelvedata_ohlcv(asset.twelvedata, "1h", min(max(hours, 100), 5000), key)}
    if start is None:
        frames["1d"] = twelvedata_ohlcv(asset.twelvedata, "1day", 730, key)
        if with_15m:
            frames["15m"] = twelvedata_ohlcv(asset.twelvedata, "15min", 2000, key)
    return frames


def get_price_history(
    asset: Asset, start: Optional[datetime] = None, with_15m: bool = False
) -> PriceData:
    """Bougies 1h, 4h et journalières (+ 15 min si `with_15m`, pour le mode SMC).

    Avec `start` (utilisé par l'historique), seules les bougies 1h sont renvoyées.
    """
    note = ""
    if config.TWELVEDATA_API_KEY and asset.twelvedata:
        try:
            frames = _twelvedata_frames(asset, start, with_15m)
            source = f"Twelve Data, spot {asset.twelvedata}"
            return PriceData(_with_4h(frames, start), source)
        except Exception as e:
            note = f" (Twelve Data indisponible : {e})"

    frames = _yahoo_frames(asset, start, with_15m)
    source = f"Yahoo Finance {asset.yahoo}"
    if asset.spot_adjust:
        spot = gold_spot_price()
        last = float(frames["1h"]["Close"].iloc[-1])
        if spot and abs(last - spot) / spot * 100 < MAX_BASIS_PCT:
            frames, basis = adjust_to_spot(frames, spot)
            source += f" recalé sur le spot (écart future/spot {basis:+.2f})"
        else:
            source += " — future NON recalé sur le spot (prix spot indisponible)"
    return PriceData(_with_4h(frames, start), source + note)


def _with_4h(frames: dict[str, pd.DataFrame], start: Optional[datetime]) -> dict:
    if start is None:
        frames = {**frames, "4h": resample(frames["1h"], "4h")}
        frames = {tf: frames[tf] for tf in ("15m", "1h", "4h", "1d") if tf in frames}
    return frames


# ------------------------------------------------------------------ macro


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


# ------------------------------------------------------------------ sentiment


def crypto_fear_greed() -> list[dict]:
    """Indice Fear & Greed crypto (alternative.me), du plus récent au plus ancien."""
    js = _get_json(CRYPTO_FEAR_GREED_URL, {"limit": 31})
    return [
        {
            "date": datetime.fromtimestamp(int(d["timestamp"]), timezone.utc).date().isoformat(),
            "valeur": int(d["value"]),
            "classe": d["value_classification"],
        }
        for d in js["data"]
    ]


def sentiment_snapshot(asset: Asset, frames: dict[str, pd.DataFrame]) -> str:
    """Données de sentiment disponibles gratuitement ; le reste vient de la recherche web."""
    lines = []
    if asset.key == "BTCUSD":
        try:
            fg = crypto_fear_greed()
            picks = [(label, fg[i]) for label, i in (("aujourd'hui", 0), ("il y a 7 jours", 7),
                                                     ("il y a 30 jours", 30)) if i < len(fg)]
            lines.append("Indice Fear & Greed crypto (0 = peur extrême, 100 = avidité extrême) :")
            lines += [f"- {label} ({d['date']}) : {d['valeur']} — {d['classe']}" for label, d in picks]
        except Exception:
            lines.append("Indice Fear & Greed crypto indisponible : cherche-le sur le web.")

    daily = frames.get("1d")
    if daily is not None and len(daily) > 252:
        close = daily["Close"]
        hi, lo = float(close.iloc[-252:].max()), float(close.iloc[-252:].min())
        pos = (float(close.iloc[-1]) - lo) / (hi - lo) * 100 if hi > lo else 50.0
        lines.append(
            f"Position du prix dans son range 52 semaines : {pos:.0f}% "
            f"(plus bas {lo:.2f}, plus haut {hi:.2f})."
        )
    return "\n".join(lines) or "Aucune donnée de sentiment pré-chargée : utilise la recherche web."
