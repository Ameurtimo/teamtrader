"""Rejeu d'un ordre sur des bougies : moteur commun à l'historique et au paper trading.

Règles (méthode « v2 ») :
- La bougie qui contient l'heure de la décision est prise en compte : ce qui s'y
  passe est signalé comme ambigu, car une partie de son range peut être antérieure
  à la décision.
- Une bougie encore en formation (non clôturée) est ignorée.
- Ordre d'entrée : exécuté à la décision si l'entrée est au prix du marché, sinon dès
  qu'une bougie touche le prix d'entrée.
- Dans la bougie d'exécution, un objectif touché ne compte pas (on ne sait pas s'il a
  été atteint avant ou après l'entrée) ; un stop touché compte comme une perte.
- Stop et objectif dans la même bougie : perte (hypothèse prudente), signalée.
- La période n'est considérée comme terminée que si les données la couvrent
  réellement : il faut une bougie au-delà de l'échéance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

METHOD_VERSION = "v2"
TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
MARKET_ENTRY_TOLERANCE = 0.001  # entrée à moins de 0,1 % du prix = exécution au marché


def drop_open_candle(df: pd.DataFrame, tf_minutes: int, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    if df.index[-1] + timedelta(minutes=tf_minutes) > now:
        return df.iloc[:-1]
    return df


def expected_closure(asset_key: str, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    """Le marché de l'or ferme le week-end et une heure par jour ; le bitcoin jamais."""
    if asset_key != "XAUUSD":
        return False
    if end - start <= timedelta(hours=2):
        return True  # pause quotidienne
    return start.weekday() == 4 and start.hour >= 19 and end.weekday() in (6, 0)


@dataclass
class Coverage:
    complete: bool
    granularity: str
    candles: int
    gaps: list[tuple[str, str]] = field(default_factory=list)
    starts_ok: bool = True

    @property
    def note(self) -> str:
        parts = [f"{self.candles} bougies {self.granularity}"]
        if not self.starts_ok:
            parts.append("les données ne couvrent pas le début de la période")
        if self.gaps:
            parts.append(f"{len(self.gaps)} trou(s) inattendu(s) dans les données")
        if not self.complete:
            parts.append("période pas encore entièrement couverte par les données")
        return ", ".join(parts)


def coverage(candles: pd.DataFrame, start: datetime, end: datetime, tf: str,
             asset_key: str, now: datetime) -> Coverage:
    tf_min = TF_MINUTES[tf]
    win = candles[(candles.index + timedelta(minutes=tf_min) > start) & (candles.index < end)]
    starts_ok = covers_start(candles, start, tf, asset_key)
    complete = starts_ok and now >= end and (
        (candles.index >= end).any()
        or (len(candles) and candles.index[-1] + timedelta(minutes=tf_min) >= end)
    )
    gaps = []
    idx = win.index
    for a, b in zip(idx, idx[1:]):
        if b - a > timedelta(minutes=2 * tf_min) and not expected_closure(asset_key, a, b):
            gaps.append((f"{a:%Y-%m-%d %H:%M}", f"{b:%Y-%m-%d %H:%M}"))
    return Coverage(bool(complete), tf, len(win), gaps, starts_ok)


def covers_start(candles: pd.DataFrame, start: datetime, tf: str, asset_key: str) -> bool:
    """Les données commencent-elles assez tôt ? Une première bougie postérieure à la
    décision n'est acceptable que si le marché était fermé entre les deux (week-end de l'or)."""
    if candles.empty:
        return False
    first = candles.index[0]
    if first <= pd.Timestamp(start) + timedelta(minutes=TF_MINUTES[tf]):
        return True
    return expected_closure(asset_key, pd.Timestamp(start), first)


@dataclass
class ReplayResult:
    status: str  # objectif | stop | expiré | en cours | non déclenché | en attente d'entrée | ...
    filled_at: Optional[datetime] = None
    exit_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    r_multiple: Optional[float] = None
    last_price: Optional[float] = None
    ambiguities: list[str] = field(default_factory=list)


def replay(*, long: bool, entry: float, stop: Optional[float], target: Optional[float],
           price_at_decision: float, created_at: datetime, expires_at: datetime,
           candles: pd.DataFrame, tf: str, complete: bool) -> ReplayResult:
    tf_delta = timedelta(minutes=TF_MINUTES[tf])
    win = candles[(candles.index + tf_delta > created_at) & (candles.index < expires_at)]
    if win.empty:
        return ReplayResult("en attente de données")
    # Bougie commencée AVANT la décision : une partie de son range lui est antérieure.
    decision_candle = win.index[0] if win.index[0] < created_at else None
    last = float(win["Close"].iloc[-1])
    risk = abs(entry - stop) if stop is not None else None
    amb: list[str] = []

    def r_of(exit_price: float) -> Optional[float]:
        if not risk:
            return None
        return ((exit_price - entry) if long else (entry - exit_price)) / risk

    # 1. Exécution de l'ordre.
    market = abs(entry - price_at_decision) / price_at_decision <= MARKET_ENTRY_TOLERANCE
    if market:
        fill_i, filled_at = 0, created_at
    else:
        hit = (win["Low"] <= entry) & (win["High"] >= entry)
        if not hit.any():
            status = "non déclenché" if complete else "en attente d'entrée"
            return ReplayResult(status, last_price=last)
        fill_i = int(hit.to_numpy().argmax())
        filled_at = win.index[fill_i]
        if filled_at == decision_candle:
            amb.append("entrée touchée dans la bougie de la décision (peut-être avant elle)")

    # 2. Stop ou objectif.
    for i in range(fill_i, len(win)):
        ts, c = win.index[i], win.iloc[i]
        stop_hit = stop is not None and (c.Low <= stop if long else c.High >= stop)
        tp_hit = target is not None and (c.High >= target if long else c.Low <= target)
        first = i == fill_i
        if stop_hit:
            if tp_hit:
                amb.append(f"stop et objectif touchés dans la même bougie ({ts:%Y-%m-%d %H:%M}) : "
                           "compté comme une perte")
            elif first and not market:
                amb.append("entrée et stop touchés dans la même bougie : compté comme une perte")
            if ts == decision_candle:
                amb.append("stop touché dans la bougie de la décision")
            return ReplayResult("stop", filled_at, ts, stop, r_of(stop), last, amb)
        if tp_hit:
            if first:
                amb.append(f"objectif touché dans la bougie d'entrée ({ts:%Y-%m-%d %H:%M}) : "
                           "non compté, ordre incertain")
                continue
            return ReplayResult("objectif", filled_at, ts, target, r_of(target), last, amb)

    # 3. Ni l'un ni l'autre.
    if complete:
        return ReplayResult("expiré", filled_at, win.index[-1] + tf_delta, last, r_of(last),
                            last, amb)
    return ReplayResult("en cours", filled_at, None, None, r_of(last), last, amb)


def window_candles(candles: pd.DataFrame, created_at: datetime, until: datetime,
                   tf: str) -> pd.DataFrame:
    tf_delta = timedelta(minutes=TF_MINUTES[tf])
    return candles[(candles.index + tf_delta > created_at) & (candles.index <= until)]


def to_records(df: pd.DataFrame) -> list[list]:
    return [[ts.isoformat(), float(r.Open), float(r.High), float(r.Low), float(r.Close)]
            for ts, r in df.iterrows()]


def from_records(rows: list[list]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"],
                            index=pd.DatetimeIndex([], tz=timezone.utc))
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["Open", "High", "Low", "Close"])


def fetch_best(fetch, asset: str, provider: str, start: datetime):
    """Bougies 15 min si possible (plus précises), sinon 1 h ; source d'origine d'abord.

    fetch(asset, provider, start, tf) renvoie des bougies brutes. Pour le bitcoin, on
    accepte Yahoo en remplacement ; pas pour l'or, dont l'écart future/spot dépend de
    la source. Renvoie (bougies ou None, tf, note).
    """
    errors = []
    providers = [provider] + (["yahoo"] if provider != "yahoo" and asset != "XAUUSD" else [])
    for prov in providers:
        for tf in ("15m", "1h"):
            try:
                df = fetch(asset, prov, start, tf)
            except Exception as e:
                errors.append(f"{prov} {tf} : {e}")
                continue
            if df is not None and not df.empty:
                if not covers_start(df, start, tf, asset):
                    errors.append(f"{prov} {tf} : ne remonte pas jusqu'au {start:%Y-%m-%d %H:%M}")
                    continue
                note = "" if prov == provider else f"source de remplacement ({prov})"
                return df, tf, note
    return None, "1h", "données indisponibles (" + " | ".join(errors[-2:]) + ")"
