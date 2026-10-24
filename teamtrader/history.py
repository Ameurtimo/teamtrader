"""Journal des décisions et mesure de leur fiabilité.

Chaque décision est enregistrée dans une base SQLite. On la rejoue ensuite sur les
bougies 1h qui ont suivi : l'ordre a-t-il été déclenché, puis l'objectif 1 ou le
stop a-t-il été touché en premier ? On mesure aussi, pour chaque analyste, si son
signal (haussier / baissier) allait dans le sens du marché sur l'horizon prévu.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from . import config

# Une entrée à moins de 0,1 % du prix au moment de la décision est considérée
# comme exécutée au marché.
MARKET_ENTRY_TOLERANCE = 0.001

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    asset TEXT NOT NULL,
    style TEXT NOT NULL,
    max_hours INTEGER NOT NULL,
    price REAL NOT NULL,
    decision TEXT NOT NULL,
    confidence INTEGER,
    entry REAL,
    stop REAL,
    targets TEXT NOT NULL,
    horizon TEXT,
    signals TEXT NOT NULL,
    source TEXT,
    payload TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'standard'
)
"""


@dataclass
class Record:
    id: int
    created_at: datetime
    asset: str
    style: str
    max_hours: int
    price: float
    decision: str
    confidence: Optional[int]
    entry: Optional[float]
    stop: Optional[float]
    targets: list[float]
    horizon: str
    signals: dict[str, Optional[str]]
    source: str = ""
    mode: str = "standard"  # standard | smc

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(hours=self.max_hours)


@dataclass
class Outcome:
    status: str
    price_move_pct: Optional[float] = None
    r_multiple: Optional[float] = None
    filled_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    window_complete: bool = False
    details: dict = field(default_factory=dict)


# ------------------------------------------------------------------ stockage


def connect(path: str = config.HISTORY_DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    # Bases créées avant l'ajout du mode SMC : on ajoute la colonne manquante.
    columns = {row[1] for row in con.execute("PRAGMA table_info(decisions)")}
    if "mode" not in columns:
        con.execute("ALTER TABLE decisions ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'")
        con.commit()
    return con


def save(con, *, asset: str, style: str, price: float, decision, reports, source: str = "",
         created_at: Optional[datetime] = None, mode: str = "standard") -> int:
    created_at = created_at or datetime.now(timezone.utc)
    signals = {r.key: r.direction for r in reports}
    cur = con.execute(
        "INSERT INTO decisions (created_at, asset, style, max_hours, price, decision, "
        "confidence, entry, stop, targets, horizon, signals, source, payload, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_at.isoformat(),
            asset,
            style,
            config.STYLE_MAX_HOURS.get(style, 240),
            price,
            decision.decision,
            decision.confiance,
            decision.prix_entree,
            decision.stop_loss,
            json.dumps(decision.take_profits),
            decision.horizon,
            json.dumps(signals),
            source,
            decision.model_dump_json(),
            mode,
        ),
    )
    con.commit()
    return cur.lastrowid


def load(con) -> list[Record]:
    rows = con.execute(
        "SELECT id, created_at, asset, style, max_hours, price, decision, confidence, entry, "
        "stop, targets, horizon, signals, source, mode FROM decisions ORDER BY created_at DESC"
    ).fetchall()
    return [
        Record(
            id=r[0], created_at=datetime.fromisoformat(r[1]), asset=r[2], style=r[3],
            max_hours=r[4], price=r[5], decision=r[6], confidence=r[7], entry=r[8], stop=r[9],
            targets=json.loads(r[10]), horizon=r[11] or "", signals=json.loads(r[12]),
            source=r[13] or "", mode=r[14],
        )
        for r in rows
    ]


def delete(con, record_id: int) -> None:
    con.execute("DELETE FROM decisions WHERE id = ?", (record_id,))
    con.commit()


# ------------------------------------------------------------------ évaluation


def evaluate(rec: Record, candles: pd.DataFrame, now: Optional[datetime] = None) -> Outcome:
    """Rejoue une décision sur les bougies 1h postérieures à sa création.

    Si une même bougie touche le stop et l'objectif, on compte une perte (on ne
    peut pas savoir lequel est arrivé en premier : hypothèse prudente).
    """
    now = now or datetime.now(timezone.utc)
    complete = now >= rec.expires_at
    window = candles[(candles.index > rec.created_at) & (candles.index <= rec.expires_at)]
    if window.empty:
        return Outcome(status="en attente de données", window_complete=complete)

    last_close = float(window["Close"].iloc[-1])
    move = (last_close / rec.price - 1) * 100
    if rec.decision == "ATTENDRE":
        return Outcome(status="pas de trade", price_move_pct=move, window_complete=complete)

    long = rec.decision == "ACHAT"
    entry = rec.entry or rec.price
    tp = rec.targets[0] if rec.targets else None

    # 1. Déclenchement de l'ordre.
    if abs(entry - rec.price) / rec.price <= MARKET_ENTRY_TOLERANCE:
        fill_idx = 0
    else:
        hit = (window["Low"] <= entry) & (window["High"] >= entry)
        if not hit.any():
            status = "non déclenché" if complete else "en attente d'entrée"
            return Outcome(status=status, price_move_pct=move, window_complete=complete)
        fill_idx = int(hit.to_numpy().argmax())
    filled_at = window.index[fill_idx]
    after = window.iloc[fill_idx:]

    # 2. Stop ou objectif.
    risk = abs(entry - rec.stop) if rec.stop else None
    for ts, c in after.iterrows():
        stop_hit = rec.stop is not None and (c.Low <= rec.stop if long else c.High >= rec.stop)
        tp_hit = tp is not None and (c.High >= tp if long else c.Low <= tp)
        if stop_hit:
            return Outcome(
                status="perdant", price_move_pct=move, r_multiple=-1.0 if risk else None,
                filled_at=filled_at, closed_at=ts, exit_price=rec.stop, window_complete=complete,
            )
        if tp_hit:
            return Outcome(
                status="gagnant", price_move_pct=move,
                r_multiple=abs(tp - entry) / risk if risk else None,
                filled_at=filled_at, closed_at=ts, exit_price=tp, window_complete=complete,
            )

    # 3. Ni l'un ni l'autre : position encore ouverte ou clôturée à l'expiration.
    signed = (last_close - entry) if long else (entry - last_close)
    return Outcome(
        status="expiré" if complete else "en cours",
        price_move_pct=move,
        r_multiple=signed / risk if risk else None,
        filled_at=filled_at,
        closed_at=window.index[-1] if complete else None,
        exit_price=last_close,
        window_complete=complete,
    )


def statistics(pairs: list[tuple[Record, Outcome]]) -> dict:
    """Taux de réussite du chef de desk et de chaque analyste."""
    closed = [(r, o) for r, o in pairs if o.status in ("gagnant", "perdant", "expiré")]
    wins = sum(1 for _, o in closed if o.status == "gagnant")
    losses = sum(1 for _, o in closed if o.status == "perdant")
    rs = [o.r_multiple for _, o in closed if o.r_multiple is not None]

    # Justesse directionnelle : sur les fenêtres terminées, le signal allait-il dans le
    # sens du marché ? Les signaux neutres ne sont pas comptés.
    agents: dict[str, list[bool]] = {}
    for r, o in pairs:
        if not o.window_complete or o.price_move_pct is None or o.price_move_pct == 0:
            continue
        up = o.price_move_pct > 0
        votes = dict(r.signals)
        votes["chef de desk"] = {"ACHAT": "HAUSSIER", "VENTE": "BAISSIER"}.get(r.decision)
        for name, direction in votes.items():
            if direction in ("HAUSSIER", "BAISSIER"):
                agents.setdefault(name, []).append((direction == "HAUSSIER") == up)

    return {
        "decisions": len(pairs),
        "trades_clotures": len(closed),
        "gagnants": wins,
        "perdants": losses,
        "taux_reussite": wins / (wins + losses) * 100 if wins + losses else None,
        "r_moyen": sum(rs) / len(rs) if rs else None,
        "r_total": sum(rs) if rs else None,
        "justesse_agents": {
            name: {"signaux": len(v), "justes": sum(v), "taux": sum(v) / len(v) * 100}
            for name, v in sorted(agents.items())
        },
    }
