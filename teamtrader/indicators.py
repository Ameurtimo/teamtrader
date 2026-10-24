"""Indicateurs techniques calculés localement (aucune dépendance externe).

L'agent technique reçoit des chiffres déjà calculés plutôt que des bougies brutes :
c'est plus fiable et beaucoup moins coûteux en tokens.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Aucune baisse sur la période : RSI = 100.
    return out.where(loss != 0, 100.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["Close"].shift()
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    return mid - k * std, mid, mid + k * std


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr_n = atr(df, n)
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / tr_n
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / tr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def swing_levels(df: pd.DataFrame, window: int = 5, count: int = 3) -> tuple[list[float], list[float]]:
    """Plus hauts / plus bas locaux récents : supports sous le prix, résistances au-dessus."""
    highs, lows = df["High"], df["Low"]
    span = 2 * window + 1
    is_high = highs == highs.rolling(span, center=True).max()
    is_low = lows == lows.rolling(span, center=True).min()
    price = float(df["Close"].iloc[-1])
    res = sorted({round(float(v), 2) for v in highs[is_high] if v > price})[:count]
    sup = sorted({round(float(v), 2) for v in lows[is_low] if v < price}, reverse=True)[:count]
    return sup, res


def summarize(df: pd.DataFrame) -> dict:
    close = df["Close"]
    price = float(close.iloc[-1])
    e20, e50, e200 = (float(ema(close, n).iloc[-1]) for n in (20, 50, 200))
    m_line, m_sig, m_hist = macd(close)
    bb_low, bb_mid, bb_up = bollinger(close)
    a = float(atr(df).iloc[-1])
    sup, res = swing_levels(df)

    if price > e50 > e200:
        trend = "haussière"
    elif price < e50 < e200:
        trend = "baissière"
    else:
        trend = "sans direction claire"

    lookback = min(len(close) - 1, 20)
    return {
        "prix": price,
        "tendance": trend,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi14": float(rsi(close).iloc[-1]),
        "macd": float(m_line.iloc[-1]),
        "macd_signal": float(m_sig.iloc[-1]),
        "macd_hist": float(m_hist.iloc[-1]),
        "macd_hist_prec": float(m_hist.iloc[-2]),
        "bb_basse": float(bb_low.iloc[-1]),
        "bb_milieu": float(bb_mid.iloc[-1]),
        "bb_haute": float(bb_up.iloc[-1]),
        "atr14": a,
        "atr_%": a / price * 100,
        "adx14": float(adx(df).iloc[-1]),
        "var_20_bougies_%": (price / float(close.iloc[-1 - lookback]) - 1) * 100,
        "plus_haut_20": float(df["High"].iloc[-20:].max()),
        "plus_bas_20": float(df["Low"].iloc[-20:].min()),
        "supports": sup,
        "resistances": res,
        "derniere_bougie": str(df.index[-1]),
    }


def format_technical(frames: dict[str, pd.DataFrame]) -> str:
    """Texte multi-unités de temps destiné à l'agent technique."""
    blocks = []
    for tf, df in frames.items():
        if len(df) < 30:
            continue
        s = summarize(df)
        blocks.append(
            f"### Unité de temps {tf} (dernière bougie : {s['derniere_bougie']})\n"
            f"- Prix : {s['prix']:.2f} — tendance EMA : {s['tendance']}\n"
            f"- EMA20 {s['ema20']:.2f} | EMA50 {s['ema50']:.2f} | EMA200 {s['ema200']:.2f}\n"
            f"- RSI14 : {s['rsi14']:.1f} | ADX14 : {s['adx14']:.1f}\n"
            f"- MACD {s['macd']:.2f} / signal {s['macd_signal']:.2f} / histogramme "
            f"{s['macd_hist']:.2f} (précédent {s['macd_hist_prec']:.2f})\n"
            f"- Bollinger : {s['bb_basse']:.2f} / {s['bb_milieu']:.2f} / {s['bb_haute']:.2f}\n"
            f"- ATR14 : {s['atr14']:.2f} ({s['atr_%']:.2f}% du prix)\n"
            f"- Variation sur 20 bougies : {s['var_20_bougies_%']:+.2f}% — "
            f"range 20 bougies : {s['plus_bas_20']:.2f} → {s['plus_haut_20']:.2f}\n"
            f"- Supports : {', '.join(f'{v:.2f}' for v in s['supports']) or 'aucun proche'}\n"
            f"- Résistances : {', '.join(f'{v:.2f}' for v in s['resistances']) or 'aucune proche'}"
        )
    return "\n\n".join(blocks)


def recent_candles(df: pd.DataFrame, n: int = 12) -> str:
    rows = ["| Date (UTC) | Ouv. | Haut | Bas | Clôt. |", "|---|---|---|---|---|"]
    for ts, r in df.tail(n).iterrows():
        rows.append(
            f"| {ts:%Y-%m-%d %H:%M} | {r.Open:.2f} | {r.High:.2f} | {r.Low:.2f} | {r.Close:.2f} |"
        )
    return "\n".join(rows)
