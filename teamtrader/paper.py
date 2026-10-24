"""Paper trading : portefeuille fictif structuré, exécutions simulées avec coûts.

Un ordre est créé à partir d'une décision (entrée, stop, objectif 1, échéance du style).
Il est rejoué sur les bougies avec le même moteur que l'historique (`replay`), puis le
résultat est valorisé avec les coûts du profil broker : spread, commission, glissement
sur les sorties au stop ou au marché, et financement par nuit de détention.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from . import config, replay, risk

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    asset TEXT NOT NULL,
    long INTEGER NOT NULL,
    lots REAL NOT NULL,
    units REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL,
    price_at_order REAL NOT NULL,
    expires_at TEXT NOT NULL,
    provider TEXT,
    basis REAL,
    profile TEXT NOT NULL,
    decision_id INTEGER,
    status TEXT NOT NULL DEFAULT 'en attente',
    filled_at TEXT,
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl REAL,
    costs REAL,
    net_pnl REAL,
    margin REAL,
    notes TEXT
)
"""

OPEN_STATUSES = ("en attente", "ouverte")


@dataclass
class Position:
    id: int
    created_at: datetime
    asset: str
    long: bool
    lots: float
    units: float
    entry: float
    stop: float
    target: Optional[float]
    price_at_order: float
    expires_at: datetime
    provider: Optional[str]
    basis: Optional[float]
    profile: risk.InstrumentProfile
    decision_id: Optional[int]
    status: str
    filled_at: Optional[str]
    closed_at: Optional[str]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    gross_pnl: Optional[float]
    costs: Optional[float]
    net_pnl: Optional[float]
    margin: float
    notes: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def as_open_risk(self) -> risk.OpenRisk:
        return risk.OpenRisk(self.asset, self.long, self.units, self.entry, self.stop, self.margin)


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or config.HISTORY_DB)
    con.execute(SCHEMA)
    return con


def _row(r) -> Position:
    return Position(
        id=r[0], created_at=datetime.fromisoformat(r[1]), asset=r[2], long=bool(r[3]),
        lots=r[4], units=r[5], entry=r[6], stop=r[7], target=r[8], price_at_order=r[9],
        expires_at=datetime.fromisoformat(r[10]), provider=r[11], basis=r[12],
        profile=risk.InstrumentProfile(**json.loads(r[13])), decision_id=r[14], status=r[15],
        filled_at=r[16], closed_at=r[17], exit_price=r[18], exit_reason=r[19], gross_pnl=r[20],
        costs=r[21], net_pnl=r[22], margin=r[23] or 0.0, notes=r[24],
    )


def load(con) -> list[Position]:
    rows = con.execute("SELECT * FROM paper_positions ORDER BY created_at DESC, id DESC").fetchall()
    return [_row(r) for r in rows]


def place_order(con, *, asset: str, long: bool, sizing: risk.Sizing, entry: float, stop: float,
                target: Optional[float], price: float, max_hours: int,
                profile: risk.InstrumentProfile, provider: Optional[str], basis: Optional[float],
                decision_id: Optional[int] = None, created_at: Optional[datetime] = None) -> int:
    if sizing.blocked or sizing.lots <= 0:
        raise ValueError("Ordre bloqué par la gestion du risque : " + " ; ".join(sizing.reasons))
    created_at = created_at or datetime.now(timezone.utc)
    cur = con.execute(
        "INSERT INTO paper_positions (created_at, asset, long, lots, units, entry, stop, target, "
        "price_at_order, expires_at, provider, basis, profile, decision_id, margin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (created_at.isoformat(), asset, int(long), sizing.lots, sizing.units, entry, stop, target,
         price, (created_at + timedelta(hours=max_hours)).isoformat(), provider, basis,
         json.dumps(asdict(profile)), decision_id, sizing.margin),
    )
    con.commit()
    return cur.lastrowid


def _nights(start: datetime, end: datetime) -> int:
    """Nombre de passages de minuit (UTC) pendant la détention : base du financement."""
    return max(0, (end.date() - start.date()).days)


def valuation(pos: Position, exit_price: float, exit_at: datetime, slipped: bool) -> dict:
    p = pos.profile
    gross = (exit_price - pos.entry) * pos.units if pos.long else (pos.entry - exit_price) * pos.units
    filled = datetime.fromisoformat(pos.filled_at) if pos.filled_at else pos.created_at
    costs = risk.round_trip_costs(p, pos.units, pos.entry, slipped)
    costs += risk.swap_cost(p, pos.long, pos.units * pos.entry, _nights(filled, exit_at))
    return {"gross_pnl": gross, "costs": costs, "net_pnl": gross - costs}


def _set(con, pid: int, **values) -> None:
    cols = ", ".join(f"{k} = ?" for k in values)
    con.execute(f"UPDATE paper_positions SET {cols} WHERE id = ?", (*values.values(), pid))
    con.commit()


def update(con, pos: Position, candles: pd.DataFrame, tf: str,
           now: Optional[datetime] = None) -> Position:
    """Fait avancer une position en attente / ouverte à partir de bougies (déjà recalées)."""
    now = now or datetime.now(timezone.utc)
    if not pos.is_open:
        return pos
    candles = replay.drop_open_candle(candles, replay.TF_MINUTES[tf], now)
    cov = replay.coverage(candles, pos.created_at, pos.expires_at, tf, pos.asset, now)
    res = replay.replay(long=pos.long, entry=pos.entry, stop=pos.stop, target=pos.target,
                        price_at_decision=pos.price_at_order, created_at=pos.created_at,
                        expires_at=pos.expires_at, candles=candles, tf=tf,
                        complete=cov.complete)
    notes = "; ".join(res.ambiguities) or None
    if res.status in ("en attente de données", "en attente d'entrée"):
        return pos
    if res.status == "non déclenché":
        _set(con, pos.id, status="annulée", exit_reason="ordre non exécuté avant l'échéance",
             closed_at=pos.expires_at.isoformat(), notes=notes)
    elif res.status in ("objectif", "stop", "expiré"):
        pos.filled_at = res.filled_at.isoformat()
        exit_at = res.exit_at or now
        val = valuation(pos, res.exit_price, exit_at, slipped=res.status != "objectif")
        _set(con, pos.id, status="clôturée", filled_at=pos.filled_at,
             closed_at=exit_at.isoformat(), exit_price=res.exit_price,
             exit_reason={"objectif": "objectif atteint", "stop": "stop touché",
                          "expiré": "échéance"}[res.status], notes=notes, **val)
    else:  # en cours
        _set(con, pos.id, status="ouverte", filled_at=res.filled_at.isoformat(), notes=notes,
             exit_price=res.last_price)  # dernier cours, pour la valorisation latente
    return next(p for p in load(con) if p.id == pos.id)


def close_at_market(con, pos: Position, price: float, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    if pos.status == "en attente":
        _set(con, pos.id, status="annulée", exit_reason="annulée manuellement",
             closed_at=now.isoformat())
        return
    val = valuation(pos, price, now, slipped=True)
    _set(con, pos.id, status="clôturée", closed_at=now.isoformat(), exit_price=price,
         exit_reason="clôture manuelle", **val)


def update_all(con, fetch, now: Optional[datetime] = None) -> list[str]:
    """Met à jour toutes les positions ouvertes. Renvoie les problèmes de données."""
    now = now or datetime.now(timezone.utc)
    problems = []
    open_pos = [p for p in load(con) if p.is_open]
    groups: dict[tuple, list[Position]] = {}
    for p in open_pos:
        groups.setdefault((p.asset, p.provider or "yahoo"), []).append(p)
    for (asset, provider), positions in groups.items():
        start = min(p.created_at for p in positions) - timedelta(hours=1)
        candles, tf, note = replay.fetch_best(fetch, asset, provider, start)
        if candles is None:
            problems.append(f"{asset} : {note}")
            continue
        for p in positions:
            adj = candles.copy()
            adj[["Open", "High", "Low", "Close"]] -= p.basis or 0.0
            update(con, p, adj, tf, now)
    return problems


# ------------------------------------------------------------------ compte


@dataclass
class Account:
    capital: float
    realized: float
    unrealized: float
    today: float
    open_risk: float
    margin_used: float

    @property
    def equity(self) -> float:
        return self.capital + self.realized

    @property
    def equity_live(self) -> float:
        return self.equity + self.unrealized


def unrealized(pos: Position) -> float:
    if pos.status != "ouverte" or pos.exit_price is None:
        return 0.0
    now = datetime.now(timezone.utc)
    return valuation(pos, pos.exit_price, now, slipped=True)["net_pnl"]


def account(positions: list[Position], settings: risk.RiskSettings,
            now: Optional[datetime] = None) -> Account:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(config.DISPLAY_TZ)
    today = now.astimezone(tz).date()
    closed = [p for p in positions if p.status == "clôturée" and p.net_pnl is not None]
    open_pos = [p for p in positions if p.is_open]
    return Account(
        capital=settings.capital,
        realized=sum(p.net_pnl for p in closed),
        unrealized=sum(unrealized(p) for p in open_pos),
        today=sum(p.net_pnl for p in closed
                  if datetime.fromisoformat(p.closed_at).astimezone(tz).date() == today),
        open_risk=sum(p.as_open_risk().risk for p in open_pos),
        margin_used=sum(p.margin for p in open_pos),
    )


def performance(positions: list[Position], capital: float) -> dict:
    closed = sorted([p for p in positions if p.status == "clôturée" and p.net_pnl is not None],
                    key=lambda p: p.closed_at)
    curve, equity, peak, dd = [], capital, capital, 0.0
    for p in closed:
        equity += p.net_pnl
        peak = max(peak, equity)
        dd = min(dd, (equity - peak) / peak * 100 if peak else 0.0)
        curve.append((p.closed_at, equity))
    wins = sum(p.net_pnl for p in closed if p.net_pnl > 0)
    losses = -sum(p.net_pnl for p in closed if p.net_pnl < 0)
    return {
        "trades": len(closed),
        "gagnants": sum(1 for p in closed if p.net_pnl > 0),
        "pnl_net": sum(p.net_pnl for p in closed),
        "couts": sum(p.costs or 0 for p in closed),
        "profit_factor": wins / losses if losses else (None if not wins else float("inf")),
        "drawdown_max_pct": dd,
        "courbe": curve,
    }


def portfolio_text(positions: list[Position]) -> str:
    """Résumé structuré des positions ouvertes, transmis aux agents."""
    open_pos = [p for p in positions if p.is_open]
    if not open_pos:
        return "Aucune position ouverte ni ordre en attente."
    lines = []
    for p in open_pos:
        sens = "ACHAT" if p.long else "VENTE"
        tgt = f", objectif {p.target:.2f}" if p.target else ""
        lines.append(f"- {p.asset} {sens} {p.lots:g} lot(s) ({p.status}) — entrée {p.entry:.2f}, "
                     f"stop {p.stop:.2f}{tgt}, échéance {p.expires_at:%Y-%m-%d %H:%M} UTC")
    return "\n".join(lines)
