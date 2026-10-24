"""Suivi de la fiabilité du prédicteur de publications économiques.

Chaque prévision est enregistrée. Une fois le chiffre publié (flux du calendrier,
recherche IA ou saisie manuelle), on la compare au consensus :
- la prévision « au-dessus / en dessous / conforme » était-elle juste ?
- la réaction annoncée de l'or et du bitcoin dans l'heure qui suit était-elle la bonne ?
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event TEXT NOT NULL,
    event_time TEXT,
    importance TEXT,
    consensus TEXT,
    previous TEXT,
    prediction TEXT NOT NULL,
    estimate TEXT,
    probability INTEGER,
    reaction_usd TEXT,
    reaction_gold TEXT,
    reaction_btc TEXT,
    actual TEXT,
    actual_source TEXT
)
"""

# Seuil de mouvement (en %) en dessous duquel la réaction est considérée comme neutre.
NEUTRAL_MOVE_PCT = {"XAUUSD": 0.10, "BTCUSD": 0.25}
MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_NUMBER = re.compile(r"^([<>]?)\s*([+-]?\d+(?:[.,]\d+)?)\s*([KMBT%]?)$", re.I)


@dataclass
class ForecastRow:
    id: int
    created_at: datetime
    event: str
    event_time: Optional[datetime]
    importance: str
    consensus: str
    previous: str
    prediction: str
    estimate: str
    probability: Optional[int]
    reaction_usd: str
    reaction_gold: str
    reaction_btc: str
    actual: Optional[str]
    actual_source: Optional[str]

    @property
    def released(self) -> bool:
        return self.event_time is not None and self.event_time <= datetime.now(timezone.utc)


# ------------------------------------------------------------------ stockage


def connect(path: str = config.HISTORY_DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    return con


def parse_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def save_forecast(con, forecast, created_at: Optional[datetime] = None) -> int:
    created_at = created_at or datetime.now(timezone.utc)
    rows = [
        (created_at.isoformat(), e.evenement, _iso(parse_utc(e.date_heure_utc)), e.importance,
         e.consensus, e.precedent, e.prevision, e.estimation, e.probabilite, e.reaction_usd,
         e.reaction_or, e.reaction_btc)
        for e in forecast.evenements
    ]
    con.executemany(
        "INSERT INTO calendar_forecasts (created_at, event, event_time, importance, consensus, "
        "previous, prediction, estimate, probability, reaction_usd, reaction_gold, reaction_btc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    return len(rows)


def _iso(t: Optional[datetime]) -> Optional[str]:
    return t.isoformat() if t else None


def load(con) -> list[ForecastRow]:
    rows = con.execute(
        "SELECT id, created_at, event, event_time, importance, consensus, previous, prediction,"
        " estimate, probability, reaction_usd, reaction_gold, reaction_btc, actual, actual_source"
        " FROM calendar_forecasts ORDER BY event_time DESC, created_at DESC"
    ).fetchall()
    return [
        ForecastRow(r[0], datetime.fromisoformat(r[1]), r[2],
                    datetime.fromisoformat(r[3]) if r[3] else None, *r[4:])
        for r in rows
    ]


def set_actual(con, row_id: int, actual: str, source: str) -> None:
    con.execute("UPDATE calendar_forecasts SET actual = ?, actual_source = ? WHERE id = ?",
                (actual.strip() or None, source, row_id))
    con.commit()


def delete(con, row_id: int) -> None:
    con.execute("DELETE FROM calendar_forecasts WHERE id = ?", (row_id,))
    con.commit()


def latest_per_event(rows: list[ForecastRow]) -> list[ForecastRow]:
    """Si une même publication a été prévue plusieurs fois, on garde la dernière prévision
    faite AVANT la publication (les autres ne comptent pas dans les statistiques)."""
    best: dict[tuple, ForecastRow] = {}
    for r in rows:
        if r.event_time is not None and r.created_at > r.event_time:
            continue
        key = (r.event.lower(), r.event_time)
        if key not in best or r.created_at > best[key].created_at:
            best[key] = r
    return sorted(best.values(), key=lambda r: r.event_time or r.created_at, reverse=True)


def pending_actuals(rows: list[ForecastRow]) -> list[ForecastRow]:
    return [r for r in latest_per_event(rows) if r.released and not r.actual]


# ------------------------------------------------------------------ comparaison


def parse_value(text: Optional[str]) -> Optional[tuple[float, int]]:
    """« 180K » → (180000, 0) ; « -0.3% » → (-0.3, 1). Renvoie (valeur, nb de décimales)."""
    if not text:
        return None
    t = text.strip().replace(" ", "").replace("−", "-")
    m = _NUMBER.match(t)
    if not m:
        return None
    number, suffix = m.group(2).replace(",", "."), m.group(3).upper()
    decimals = len(number.split(".")[1]) if "." in number else 0
    return float(number) * MULTIPLIERS.get(suffix, 1.0), decimals


def outcome(actual: Optional[str], consensus: Optional[str]) -> Optional[str]:
    """Position du chiffre publié par rapport au consensus, à la précision du consensus."""
    a, c = parse_value(actual), parse_value(consensus)
    if a is None or c is None:
        return None
    (av, _), (cv, decimals) = a, c
    scale = MULTIPLIERS.get((consensus or "").strip()[-1:].upper(), 1.0)
    tolerance = 0.5 * 10 ** (-decimals) * scale
    if abs(av - cv) < tolerance - 1e-12:
        return "CONFORME"
    return "AU-DESSUS" if av > cv else "EN DESSOUS"


def market_move(candles: pd.DataFrame, event_time: datetime, hours: int = 1) -> Optional[float]:
    """Variation (en %) entre l'ouverture de la bougie de la publication et la clôture
    `hours` heures plus tard."""
    if candles is None or candles.empty:
        return None
    start = event_time.replace(minute=0, second=0, microsecond=0)
    window = candles[(candles.index >= start) & (candles.index < start + timedelta(hours=hours))]
    if window.empty:
        return None
    return (float(window["Close"].iloc[-1]) / float(window["Open"].iloc[0]) - 1) * 100


def move_direction(move: Optional[float], asset_key: str) -> Optional[str]:
    if move is None:
        return None
    if abs(move) < NEUTRAL_MOVE_PCT[asset_key]:
        return "NEUTRE"
    return "HAUSSIER" if move > 0 else "BAISSIER"


@dataclass
class Evaluation:
    row: ForecastRow
    outcome: Optional[str]
    correct: Optional[bool]
    gold_move: Optional[float] = None
    btc_move: Optional[float] = None
    gold_ok: Optional[bool] = None
    btc_ok: Optional[bool] = None


def evaluate(rows: list[ForecastRow], candles: dict[str, pd.DataFrame]) -> list[Evaluation]:
    out = []
    for r in latest_per_event(rows):
        res = outcome(r.actual, r.consensus)
        ev = Evaluation(r, res, None if res is None else res == r.prediction)
        if r.released and r.event_time is not None:
            ev.gold_move = market_move(candles.get("XAUUSD"), r.event_time)
            ev.btc_move = market_move(candles.get("BTCUSD"), r.event_time)
            gd = move_direction(ev.gold_move, "XAUUSD")
            bd = move_direction(ev.btc_move, "BTCUSD")
            ev.gold_ok = None if gd is None else gd == r.reaction_gold
            ev.btc_ok = None if bd is None else bd == r.reaction_btc
        out.append(ev)
    return out


def _rate(values: list[Optional[bool]]) -> Optional[dict]:
    v = [x for x in values if x is not None]
    return {"n": len(v), "justes": sum(v), "taux": sum(v) / len(v) * 100} if v else None


def statistics(evals: list[Evaluation]) -> dict:
    resolved = [e for e in evals if e.correct is not None]
    buckets = {}
    for label, lo, hi in (("< 55 %", 0, 55), ("55-65 %", 55, 65), ("≥ 65 %", 65, 101)):
        sel = [e.correct for e in resolved if lo <= (e.row.probability or 0) < hi]
        buckets[label] = _rate(sel)
    return {
        "prevision": _rate([e.correct for e in resolved]),
        "prevision_hors_conforme": _rate([e.correct for e in resolved
                                          if e.row.prediction != "CONFORME"]),
        "par_probabilite": buckets,
        "reaction_or": _rate([e.gold_ok for e in evals]),
        "reaction_btc": _rate([e.btc_ok for e in evals]),
    }


def track_record_text(stats: dict) -> str:
    """Résumé transmis à l'économiste pour qu'il calibre ses probabilités."""
    p = stats["prevision"]
    if not p or p["n"] < 5:
        return ""
    lines = [f"- Prévisions au-dessus / en dessous / conforme justes : {p['justes']}/{p['n']} "
             f"({p['taux']:.0f} %)."]
    for label, b in stats["par_probabilite"].items():
        if b:
            lines.append(f"- Quand tu annonçais une probabilité {label} : {b['taux']:.0f} % "
                         f"de réussite sur {b['n']} prévisions.")
    for key, name in (("reaction_or", "de l'or"), ("reaction_btc", "du bitcoin")):
        if stats[key]:
            lines.append(f"- Réaction {name} dans l'heure correctement annoncée : "
                         f"{stats[key]['taux']:.0f} % ({stats[key]['n']} cas).")
    lines.append("Ajuste tes probabilités en conséquence : si tu as été trop confiant, sois "
                 "plus prudent.")
    return "\n".join(lines)


def match_feed_actuals(rows: list[ForecastRow], events) -> dict[int, str]:
    """Associe les chiffres publiés présents dans le flux du calendrier aux prévisions."""
    found = {}
    for r in pending_actuals(rows):
        same_time = [e for e in events if e.actual and r.event_time
                     and abs((e.time - r.event_time).total_seconds()) < 60]
        named = [e for e in same_time if e.title.lower() == r.event.lower()]
        pick = named or (same_time if len(same_time) == 1 else [])
        if pick:
            found[r.id] = pick[0].actual
    return found
