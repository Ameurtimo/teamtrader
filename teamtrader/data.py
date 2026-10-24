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

from . import config, quality
from .config import MACRO_TICKERS, Asset

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
HTTP_TIMEOUT = 15
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
GOLD_SPOT_URL = "https://api.gold-api.com/price/XAU"
CRYPTO_FEAR_GREED_URL = "https://api.alternative.me/fng/"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
OKX_OI_URL = "https://www.okx.com/api/v5/public/open-interest"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"
# Rapport COT « legacy », contrats à terme seuls (API publique de la CFTC).
CFTC_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFTC_GOLD_CODE = "088691"  # Or, COMEX
# Au-delà de cet écart future/spot, on soupçonne une donnée aberrante et on ne recale pas.
MAX_BASIS_PCT = 3.0


class MarketDataError(RuntimeError):
    pass


@dataclass
class PriceData:
    frames: dict[str, pd.DataFrame]
    source: str
    provider: str = "yahoo"  # yahoo | twelvedata
    # Écart retranché aux bougies brutes (future − spot pour l'or via Yahoo), 0 sinon.
    # Conservé avec chaque décision pour réévaluer plus tard avec le MÊME écart.
    basis: float = 0.0


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
            try:
                frames["15m"] = fetch_ohlcv(asset.yahoo, period="30d", interval="15m")
            except Exception:
                pass  # signalé par le contrôle de qualité
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
            try:
                frames["15m"] = twelvedata_ohlcv(asset.twelvedata, "15min", 2000, key)
            except Exception:
                pass  # signalé par le contrôle de qualité
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
            return PriceData(_with_4h(frames, start), source, "twelvedata", 0.0)
        except Exception as e:
            note = f" (Twelve Data indisponible : {e})"

    frames = _yahoo_frames(asset, start, with_15m)
    source = f"Yahoo Finance {asset.yahoo}"
    basis = 0.0
    if asset.spot_adjust:
        spot = gold_spot_price()
        last = float(frames["1h"]["Close"].iloc[-1])
        if spot and abs(last - spot) / spot * 100 < MAX_BASIS_PCT:
            frames, basis = adjust_to_spot(frames, spot)
            source += f" recalé sur le spot (écart future/spot {basis:+.2f})"
        else:
            source += " — future NON recalé sur le spot (prix spot indisponible)"
    return PriceData(_with_4h(frames, start), source + note, "yahoo", basis)


def current_basis(asset: Asset) -> float:
    """Écart future − spot actuel (0 si l'actif n'est pas recalé ou si le spot manque)."""
    if not asset.spot_adjust:
        return 0.0
    spot = gold_spot_price()
    if not spot:
        return 0.0
    last = float(fetch_ohlcv(asset.yahoo, period="5d", interval="1h")["Close"].iloc[-1])
    return last - spot if abs(last - spot) / spot * 100 < MAX_BASIS_PCT else 0.0


def get_candles(asset: Asset, provider: str, start: datetime, tf: str) -> pd.DataFrame:
    """Bougies BRUTES (non recalées) d'un fournisseur précis, depuis `start`.

    Sert à réévaluer une décision avec la même source et le même écart future/spot
    qu'au moment où elle a été prise. tf : « 15m » ou « 1h ».
    """
    if provider == "twelvedata":
        if not (config.TWELVEDATA_API_KEY and asset.twelvedata):
            raise MarketDataError("Clé Twelve Data absente : source d'origine indisponible.")
        minutes = 15 if tf == "15m" else 60
        n = int((datetime.now(timezone.utc) - start) / timedelta(minutes=minutes)) + 8
        if n > 5000:
            raise MarketDataError(f"Période trop longue pour des bougies {tf} Twelve Data.")
        df = twelvedata_ohlcv(asset.twelvedata, "15min" if tf == "15m" else "1h",
                              max(n, 50), config.TWELVEDATA_API_KEY)
        return df[df.index >= start]
    if tf == "15m" and datetime.now(timezone.utc) - start > timedelta(days=58):
        raise MarketDataError("Yahoo ne fournit des bougies 15 min que sur 60 jours.")
    return fetch_ohlcv(asset.yahoo, interval=tf, start=start)


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
                "date": close.index[-1].to_pydatetime(),
            }
        )
    return rows


def macro_item(rows: list[dict], now: Optional[datetime] = None) -> quality.DataItem:
    total = len(MACRO_TICKERS)
    if not rows:
        return quality.DataItem("Tableau inter-marchés", "Yahoo Finance", quality.MISSING,
                                detail=f"0/{total} indicateurs")
    oldest = min(r["date"] for r in rows)
    # Clôtures journalières : jusqu'à 4 jours d'ancienneté (week-ends et jours fériés).
    item = quality.freshness("Tableau inter-marchés", "Yahoo Finance", oldest + timedelta(days=1),
                             4 * 24, now, detail=f"{len(rows)}/{total} indicateurs")
    if len(rows) < total * 0.6 and item.status == quality.OK:
        item.status = quality.MISSING
    return item


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


def _okx(url: str, params: dict) -> dict:
    js = _get_json(url, params)
    if str(js.get("code")) != "0" or not js.get("data"):
        raise MarketDataError(f"OKX : {js.get('msg') or 'réponse vide'}")
    return js["data"][0]


def crypto_derivatives() -> dict:
    """Taux de financement et open interest du perpétuel BTC (OKX, sinon Binance)."""
    try:
        f = _okx(OKX_FUNDING_URL, {"instId": "BTC-USDT-SWAP"})
        oi = _okx(OKX_OI_URL, {"instType": "SWAP", "instId": "BTC-USDT-SWAP"})
        return {
            "funding_pct": float(f["fundingRate"]) * 100,
            "open_interest_btc": float(oi.get("oiCcy") or 0) or None,
            "as_of": datetime.fromtimestamp(int(oi["ts"]) / 1000, timezone.utc),
            "source": "OKX BTC-USDT-SWAP",
        }
    except Exception as okx_error:
        try:
            f = _get_json(BINANCE_FUNDING_URL, {"symbol": "BTCUSDT"})
            oi = _get_json(BINANCE_OI_URL, {"symbol": "BTCUSDT"})
            return {
                "funding_pct": float(f["lastFundingRate"]) * 100,
                "open_interest_btc": float(oi["openInterest"]),
                "as_of": datetime.fromtimestamp(int(oi["time"]) / 1000, timezone.utc),
                "source": "Binance BTCUSDT perpétuel",
            }
        except Exception as binance_error:
            raise MarketDataError(f"OKX : {okx_error} | Binance : {binance_error}")


def gold_cot() -> dict:
    """Position nette des non-commerciaux (spéculateurs) sur l'or COMEX, rapport COT CFTC."""
    rows = _get_json(CFTC_COT_URL, {
        "$where": f"cftc_contract_market_code='{CFTC_GOLD_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 5,
    })
    if not rows:
        raise MarketDataError("CFTC : aucun rapport COT reçu.")

    def net(r):
        return float(r["noncomm_positions_long_all"]) - float(r["noncomm_positions_short_all"])

    last = rows[0]
    out = {
        "date": datetime.fromisoformat(last["report_date_as_yyyy_mm_dd"][:10]).replace(
            tzinfo=timezone.utc),
        "net": net(last),
        "long": float(last["noncomm_positions_long_all"]),
        "short": float(last["noncomm_positions_short_all"]),
        "open_interest": float(last.get("open_interest_all") or 0) or None,
        "source": "CFTC, rapport COT (non-commerciaux, futures seuls)",
    }
    if len(rows) > 1:
        out["net_change_1w"] = out["net"] - net(rows[1])
    if len(rows) > 4:
        out["net_change_4w"] = out["net"] - net(rows[4])
    return out


def sentiment_data(asset: Asset, frames: dict[str, pd.DataFrame],
                   now: Optional[datetime] = None) -> tuple[str, list[quality.DataItem]]:
    """Données de sentiment structurées et datées ; le reste vient de la recherche web."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []
    items: list[quality.DataItem] = []
    if asset.key == "BTCUSD":
        try:
            fg = crypto_fear_greed()
            picks = [(label, fg[i]) for label, i in (("aujourd'hui", 0), ("il y a 7 jours", 7),
                                                     ("il y a 30 jours", 30)) if i < len(fg)]
            lines.append("Indice Fear & Greed crypto (0 = peur extrême, 100 = avidité extrême) :")
            lines += [f"- {label} ({d['date']}) : {d['valeur']} — {d['classe']}" for label, d in picks]
            as_of = datetime.fromisoformat(fg[0]["date"]).replace(tzinfo=timezone.utc)
            items.append(quality.freshness("Fear & Greed crypto", "alternative.me",
                                           as_of + timedelta(days=1), 48, now))
        except Exception:
            lines.append("Indice Fear & Greed crypto indisponible : cherche-le sur le web.")
            items.append(quality.DataItem("Fear & Greed crypto", "alternative.me", quality.MISSING))
        try:
            d = crypto_derivatives()
            oi = f", open interest {d['open_interest_btc']:,.0f} BTC" if d["open_interest_btc"] else ""
            lines.append(f"Perpétuel BTC ({d['source']}, {d['as_of']:%Y-%m-%d %H:%M} UTC) : "
                         f"taux de financement {d['funding_pct']:+.4f} % par période de 8 h{oi}.")
            items.append(quality.freshness("Funding / open interest BTC", d["source"],
                                           d["as_of"], 12, now))
        except Exception as e:
            lines.append("Funding et open interest indisponibles : cherche-les sur le web.")
            items.append(quality.DataItem("Funding / open interest BTC", "OKX / Binance",
                                          quality.MISSING, detail=str(e)[:120]))
    if asset.key == "XAUUSD":
        try:
            c = gold_cot()
            ch1 = f", variation sur 1 semaine {c['net_change_1w']:+,.0f}" if "net_change_1w" in c else ""
            ch4 = f", sur 4 semaines {c['net_change_4w']:+,.0f}" if "net_change_4w" in c else ""
            lines.append(f"COT or ({c['source']}, rapport du {c['date']:%Y-%m-%d}) : position nette "
                         f"des spéculateurs {c['net']:+,.0f} contrats (longs {c['long']:,.0f}, "
                         f"shorts {c['short']:,.0f}){ch1}{ch4}.")
            # Publié le vendredi pour le mardi : jusqu'à 10 jours d'ancienneté est normal.
            items.append(quality.freshness("COT or", "CFTC", c["date"], 10 * 24, now))
        except Exception as e:
            lines.append("Rapport COT indisponible : cherche-le sur le web.")
            items.append(quality.DataItem("COT or", "CFTC", quality.MISSING, detail=str(e)[:120]))

    daily = frames.get("1d")
    if daily is not None and len(daily) > 252:
        close = daily["Close"]
        hi, lo = float(close.iloc[-252:].max()), float(close.iloc[-252:].min())
        pos = (float(close.iloc[-1]) - lo) / (hi - lo) * 100 if hi > lo else 50.0
        lines.append(
            f"Position du prix dans son range 52 semaines : {pos:.0f}% "
            f"(plus bas {lo:.2f}, plus haut {hi:.2f})."
        )
    text = "\n".join(lines) or "Aucune donnée de sentiment pré-chargée : utilise la recherche web."
    return text, items


def sentiment_snapshot(asset: Asset, frames: dict[str, pd.DataFrame]) -> str:
    return sentiment_data(asset, frames)[0]
