"""Journal des décisions et mesure de leur fiabilité.

Chaque décision est enregistrée dans une base SQLite avec la source de prix et
l'écart future/spot utilisés à ce moment-là. On la rejoue ensuite sur les bougies qui
ont suivi (15 min quand c'est possible, sinon 1 h) avec le moteur `replay`.

Reproductibilité : dès que les données couvrent toute la période de la décision, le
résultat est FIGÉ dans la base avec les bougies utilisées et la version de la méthode.
Il ne change plus ensuite, même si la source ou l'écart future/spot évoluent. Si la
méthode change de version, le résultat est recalculé à partir des bougies conservées.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from . import config, replay

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

# Colonnes ajoutées après la première version : créées à l'ouverture si absentes.
MIGRATIONS = {
    "mode": "TEXT NOT NULL DEFAULT 'standard'",
    "provider": "TEXT",
    "basis": "REAL",
    "outcome": "TEXT",
    "candles": "BLOB",
    "analysis": "TEXT",
}

# Statut « historique » affiché pour chaque résultat du moteur de rejeu.
CLOSED = ("objectif", "stop", "expiré")


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
    provider: Optional[str] = None  # None : décision antérieure au suivi de la source
    basis: Optional[float] = None
    frozen: Optional["Outcome"] = None
    analysis: Optional[dict] = None

    @property
    def expires_at(self) -> datetime:
        return self.created_at + timedelta(hours=self.max_hours)


@dataclass
class Outcome:
    status: str
    price_move_pct: Optional[float] = None
    r_multiple: Optional[float] = None
    filled_at: Optional[str] = None
    closed_at: Optional[str] = None
    exit_price: Optional[float] = None
    window_complete: bool = False
    ambiguities: list[str] = field(default_factory=list)
    data_note: str = ""
    method: str = field(default_factory=lambda: replay.METHOD_VERSION)
    frozen: bool = False

    @property
    def is_trade(self) -> bool:
        return self.status in CLOSED or self.status == "en cours"


# ------------------------------------------------------------------ stockage


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or config.HISTORY_DB)
    con.execute(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(decisions)")}
    for name, ddl in MIGRATIONS.items():
        if name not in columns:
            con.execute(f"ALTER TABLE decisions ADD COLUMN {name} {ddl}")
    con.commit()
    return con


def save(con, *, asset: str, style: str, price: float, decision, reports, source: str = "",
         created_at: Optional[datetime] = None, mode: str = "standard",
         provider: Optional[str] = None, basis: Optional[float] = None,
         analysis: Optional[dict] = None) -> int:
    created_at = created_at or datetime.now(timezone.utc)
    signals = {r.key: r.direction for r in reports}
    cur = con.execute(
        "INSERT INTO decisions (created_at, asset, style, max_hours, price, decision, "
        "confidence, entry, stop, targets, horizon, signals, source, payload, mode, provider, "
        "basis, analysis) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_at.isoformat(), asset, style, config.STYLE_MAX_HOURS.get(style, 240), price,
            decision.decision, decision.confiance, decision.prix_entree, decision.stop_loss,
            json.dumps(decision.take_profits), decision.horizon, json.dumps(signals), source,
            decision.model_dump_json(), mode, provider, basis,
            json.dumps(analysis, ensure_ascii=False, default=str) if analysis else None,
        ),
    )
    con.commit()
    return cur.lastrowid


COLUMNS = ("id, created_at, asset, style, max_hours, price, decision, confidence, entry, stop, "
           "targets, horizon, signals, source, mode, provider, basis, outcome, analysis")


def _record(r) -> Record:
    frozen = Outcome(**json.loads(r[17])) if r[17] else None
    return Record(
        id=r[0], created_at=datetime.fromisoformat(r[1]), asset=r[2], style=r[3],
        max_hours=r[4], price=r[5], decision=r[6], confidence=r[7], entry=r[8], stop=r[9],
        targets=json.loads(r[10]), horizon=r[11] or "", signals=json.loads(r[12]),
        source=r[13] or "", mode=r[14], provider=r[15], basis=r[16], frozen=frozen,
        analysis=json.loads(r[18]) if r[18] else None,
    )


def load(con) -> list[Record]:
    rows = con.execute(f"SELECT {COLUMNS} FROM decisions ORDER BY created_at DESC").fetchall()
    return [_record(r) for r in rows]


def delete(con, record_id: int) -> None:
    con.execute("DELETE FROM decisions WHERE id = ?", (record_id,))
    con.commit()


def freeze(con, rec: Record, outcome: Outcome, window: pd.DataFrame) -> None:
    outcome.frozen = True
    blob = zlib.compress(json.dumps(replay.to_records(window)).encode())
    con.execute("UPDATE decisions SET outcome = ?, candles = ? WHERE id = ?",
                (json.dumps(asdict(outcome), ensure_ascii=False), blob, rec.id))
    con.commit()
    rec.frozen = outcome


def stored_candles(con, record_id: int) -> pd.DataFrame:
    row = con.execute("SELECT candles FROM decisions WHERE id = ?", (record_id,)).fetchone()
    if not row or not row[0]:
        return replay.from_records([])
    return replay.from_records(json.loads(zlib.decompress(row[0])))


# ------------------------------------------------------------------ évaluation


def evaluate(rec: Record, candles: pd.DataFrame, now: Optional[datetime] = None,
             tf: str = "1h") -> Outcome:
    """Rejoue une décision sur des bougies (déjà dans la même référence de prix)."""
    now = now or datetime.now(timezone.utc)
    candles = replay.drop_open_candle(candles, replay.TF_MINUTES[tf], now)
    cov = replay.coverage(candles, rec.created_at, rec.expires_at, tf, rec.asset, now)
    window = replay.window_candles(candles, rec.created_at, rec.expires_at, tf)
    window = window[window.index < rec.expires_at]
    if not cov.starts_ok:
        return Outcome("en attente de données", window_complete=False, data_note=cov.note)
    if window.empty:
        return Outcome("en attente de données", window_complete=cov.complete,
                       data_note=cov.note)
    move = (float(window["Close"].iloc[-1]) / rec.price - 1) * 100
    if rec.decision == "ATTENDRE":
        return Outcome("pas de trade", price_move_pct=move, window_complete=cov.complete,
                       data_note=cov.note)

    res = replay.replay(
        long=rec.decision == "ACHAT", entry=rec.entry or rec.price, stop=rec.stop,
        target=rec.targets[0] if rec.targets else None, price_at_decision=rec.price,
        created_at=rec.created_at, expires_at=rec.expires_at, candles=candles, tf=tf,
        complete=cov.complete,
    )
    return Outcome(
        status=res.status, price_move_pct=move, r_multiple=res.r_multiple,
        filled_at=res.filled_at.isoformat() if res.filled_at else None,
        closed_at=res.exit_at.isoformat() if res.exit_at else None,
        exit_price=res.exit_price, window_complete=cov.complete,
        ambiguities=res.ambiguities, data_note=cov.note,
    )


Fetcher = Callable[[str, str, datetime, str], pd.DataFrame]


def evaluate_all(con, records: list[Record], fetch: Fetcher,
                 legacy_basis: Callable[[str], float] = lambda asset: 0.0,
                 now: Optional[datetime] = None) -> list[tuple[Record, Outcome]]:
    """Évalue toutes les décisions, en figeant celles dont la période est couverte.

    fetch(asset, provider, start, tf) renvoie des bougies BRUTES de ce fournisseur.
    legacy_basis(asset) : écart future/spot à utiliser pour les anciennes décisions qui
    ne l'ont pas enregistré (approximation, signalée).
    """
    now = now or datetime.now(timezone.utc)
    pairs: list[tuple[Record, Outcome]] = []
    todo: list[Record] = []
    for r in records:
        if r.frozen and r.frozen.method == replay.METHOD_VERSION:
            pairs.append((r, r.frozen))
        elif r.frozen:
            # Méthode modifiée : on recalcule à partir des bougies conservées.
            window = stored_candles(con, r.id)
            out = evaluate(r, _with_tail(window, r), now, _tf_of(r.frozen.data_note))
            out.data_note += " — recalculé à partir des bougies conservées"
            freeze(con, r, out, window)
            pairs.append((r, out))
        else:
            todo.append(r)

    groups: dict[tuple, list[Record]] = {}
    for r in todo:
        groups.setdefault((r.asset, r.provider or "yahoo"), []).append(r)
    for (asset, provider), recs in groups.items():
        start = min(r.created_at for r in recs) - timedelta(hours=1)
        candles, tf, note = replay.fetch_best(fetch, asset, provider, start)
        for r in recs:
            if candles is None:
                pairs.append((r, Outcome("en attente de données", data_note=note)))
                continue
            basis = r.basis
            extra = note
            if basis is None:
                basis = legacy_basis(asset) if provider == "yahoo" else 0.0
                if basis:
                    extra += " ; écart future/spot d'origine inconnu (ancienne décision) : " \
                             "estimé avec l'écart actuel"
            adjusted = candles.copy()
            adjusted[["Open", "High", "Low", "Close"]] -= basis
            out = evaluate(r, adjusted, now, tf)
            if extra:
                out.data_note = f"{out.data_note} ; {extra.strip(' ;')}"
            if out.window_complete and out.status != "en attente de données":
                window = replay.window_candles(adjusted, r.created_at, r.expires_at, tf)
                freeze(con, r, out, window)
            pairs.append((r, out))
    return sorted(pairs, key=lambda p: p[0].created_at, reverse=True)


def _tf_of(note: str) -> str:
    return "15m" if "bougies 15m" in note else "1h"


def _with_tail(window: pd.DataFrame, rec: Record) -> pd.DataFrame:
    """Bougies conservées + une bougie fictive après l'échéance pour marquer la couverture."""
    if window.empty:
        return window
    tail = pd.DataFrame([window.iloc[-1].to_dict()], index=[pd.Timestamp(rec.expires_at)])
    return pd.concat([window, tail])


# ------------------------------------------------------------------ statistiques


def _drawdown(curve: list[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return dd


def _trade_stats(pairs: list[tuple[Record, Outcome]]) -> dict:
    closed = [(r, o) for r, o in pairs if o.status in CLOSED and o.r_multiple is not None]
    rs = [o.r_multiple for _, o in closed]
    gains = sum(x for x in rs if x > 0)
    losses = -sum(x for x in rs if x < 0)
    tp = sum(1 for _, o in closed if o.status == "objectif")
    sl = sum(1 for _, o in closed if o.status == "stop")
    ordered = sorted(closed, key=lambda p: p[1].closed_at or p[0].created_at.isoformat())
    curve, total = [], 0.0
    for _, o in ordered:
        total += o.r_multiple
        curve.append(total)
    return {
        "trades_clotures": len(closed),
        "objectifs": tp,
        "stops": sl,
        "expires_gagnants": sum(1 for _, o in closed if o.status == "expiré" and o.r_multiple > 0),
        "expires_perdants": sum(1 for _, o in closed if o.status == "expiré" and o.r_multiple <= 0),
        "taux_objectif": tp / (tp + sl) * 100 if tp + sl else None,
        "taux_gagnants": sum(1 for x in rs if x > 0) / len(rs) * 100 if rs else None,
        "r_moyen": sum(rs) / len(rs) if rs else None,
        "r_total": sum(rs) if rs else None,
        "profit_factor": gains / losses if losses else (None if not gains else float("inf")),
        "drawdown_max_r": _drawdown(curve) if curve else None,
        "courbe": [(p[1].closed_at, v) for p, v in zip(ordered, curve)],
    }


def statistics(pairs: list[tuple[Record, Outcome]]) -> dict:
    stats = _trade_stats(pairs)
    stats["decisions"] = len(pairs)
    stats["en_cours"] = sum(1 for _, o in pairs if o.status == "en cours")
    stats["non_declenches"] = sum(1 for _, o in pairs if o.status == "non déclenché")
    stats["ambigus"] = sum(1 for _, o in pairs if o.ambiguities)
    # Compatibilité avec l'ancien affichage.
    stats["gagnants"], stats["perdants"] = stats["objectifs"], stats["stops"]
    stats["taux_reussite"] = stats["taux_objectif"]

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
    stats["justesse_agents"] = {
        name: {"signaux": len(v), "justes": sum(v), "taux": sum(v) / len(v) * 100}
        for name, v in sorted(agents.items())
    }

    # Ventilation par actif, style et mode.
    for key, attr in (("par_actif", "asset"), ("par_style", "style"), ("par_mode", "mode")):
        groups: dict[str, list] = {}
        for r, o in pairs:
            groups.setdefault(getattr(r, attr), []).append((r, o))
        stats[key] = {k: _trade_stats(v) for k, v in sorted(groups.items())}

    # Calibration : la confiance annoncée correspond-elle aux résultats ?
    buckets = (("< 50 %", 0, 50), ("50-64 %", 50, 65), ("65-79 %", 65, 80), ("≥ 80 %", 80, 101))
    stats["calibration"] = {}
    for label, lo, hi in buckets:
        sel = [(r, o) for r, o in pairs if lo <= (r.confidence or 0) < hi]
        ts = _trade_stats(sel)
        if ts["trades_clotures"]:
            stats["calibration"][label] = ts
    return stats
