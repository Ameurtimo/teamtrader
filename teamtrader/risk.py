"""Gestion du risque : coûts d'exécution, contraintes du broker et limites du compte.

Les valeurs par défaut des profils sont des ORDRES DE GRANDEUR typiques d'un broker CFD :
remplace-les par celles de ton broker (onglet Paper trading → Profil broker).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from typing import Optional


@dataclass
class InstrumentProfile:
    contract_size: float          # unités par lot (or : 100 oz, BTC : 1 BTC)
    min_lot: float                # lot minimum
    lot_step: float               # pas de quantité
    spread: float                 # écart achat / vente typique, en prix
    slippage: float               # glissement moyen sur un stop ou un ordre au marché, en prix
    commission_per_lot: float     # $ aller-retour par lot
    commission_pct: float         # % du nominal, aller-retour
    swap_long_pct: float          # financement annuel d'une position acheteuse (% du nominal)
    swap_short_pct: float         # idem vendeuse (négatif = coût)
    margin_pct: float             # marge exigée par le broker (% du nominal)


DEFAULT_PROFILES = {
    "XAUUSD": InstrumentProfile(contract_size=100, min_lot=0.01, lot_step=0.01, spread=0.30,
                                slippage=0.20, commission_per_lot=7.0, commission_pct=0.0,
                                swap_long_pct=-6.0, swap_short_pct=1.0, margin_pct=5.0),
    "BTCUSD": InstrumentProfile(contract_size=1, min_lot=0.01, lot_step=0.01, spread=20.0,
                                slippage=15.0, commission_per_lot=0.0, commission_pct=0.10,
                                swap_long_pct=-12.0, swap_short_pct=-12.0, margin_pct=50.0),
}


@dataclass
class RiskSettings:
    capital: float = 10_000.0           # capital de départ du compte (paper trading)
    risk_pct: float = 1.0               # risque par trade, en % des fonds propres
    max_leverage: float = 10.0          # nominal total / fonds propres
    max_open_risk_pct: float = 3.0      # risque cumulé des positions ouvertes
    daily_loss_limit_pct: float = 3.0   # au-delà : plus de nouvelle prise de risque ce jour
    profiles: dict[str, InstrumentProfile] = field(
        default_factory=lambda: {k: InstrumentProfile(**asdict(v))
                                 for k, v in DEFAULT_PROFILES.items()})

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "RiskSettings":
        raw = json.loads(text)
        profiles = dict(cls().profiles)
        names = {f.name for f in fields(InstrumentProfile)}
        for k, v in (raw.pop("profiles", None) or {}).items():
            profiles[k] = InstrumentProfile(**{**asdict(profiles.get(k, DEFAULT_PROFILES["XAUUSD"])),
                                               **{n: v[n] for n in names if n in v}})
        known = {f.name for f in fields(cls)} - {"profiles"}
        return cls(**{k: v for k, v in raw.items() if k in known}, profiles=profiles)


SETTINGS_SCHEMA = "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def load_settings(con) -> RiskSettings:
    con.execute(SETTINGS_SCHEMA)
    row = con.execute("SELECT value FROM settings WHERE key = 'risk'").fetchone()
    return RiskSettings.from_json(row[0]) if row else RiskSettings()


def save_settings(con, settings: RiskSettings) -> None:
    con.execute(SETTINGS_SCHEMA)
    con.execute("INSERT INTO settings (key, value) VALUES ('risk', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (settings.to_json(),))
    con.commit()


# ------------------------------------------------------------------ coûts


def commission(p: InstrumentProfile, lots: float, notional: float) -> float:
    return p.commission_per_lot * lots + p.commission_pct / 100 * notional


def round_trip_costs(p: InstrumentProfile, units: float, entry: float, slipped: bool) -> float:
    """Spread + commission (+ glissement si la sortie se fait sur un stop / au marché)."""
    lots = units / p.contract_size
    cost = p.spread * units + commission(p, lots, units * entry)
    if slipped:
        cost += p.slippage * units
    return cost


def swap_cost(p: InstrumentProfile, long: bool, notional: float, nights: int) -> float:
    rate = p.swap_long_pct if long else p.swap_short_pct
    return -rate / 100 / 365 * notional * nights  # taux négatif = coût positif


# ------------------------------------------------------------------ taille de position


@dataclass
class OpenRisk:
    """Position ouverte ou ordre en attente, vu par la gestion du risque."""
    asset: str
    long: bool
    units: float
    entry: float
    stop: Optional[float]
    margin: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop) * self.units if self.stop else self.entry * self.units


@dataclass
class Sizing:
    lots: float = 0.0
    units: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    leverage: float = 0.0
    risk_at_stop: float = 0.0       # perte si le stop est touché, coûts compris
    costs: float = 0.0              # spread + commission + glissement estimés
    gross_rr: Optional[float] = None
    net_rr: Optional[float] = None  # ratio après coûts
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)   # pourquoi c'est bloqué
    warnings: list[str] = field(default_factory=list)  # ajustements effectués


def size_position(*, asset: str, long: bool, entry: float, stop: float,
                  target: Optional[float], settings: RiskSettings, equity: float,
                  open_positions: list[OpenRisk], realized_today: float) -> Sizing:
    p = settings.profiles[asset]
    out = Sizing()

    def block(msg: str) -> Sizing:
        out.blocked = True
        out.reasons.append(msg)
        return out

    if equity <= 0:
        return block("Fonds propres nuls ou négatifs.")
    if realized_today <= -settings.daily_loss_limit_pct / 100 * equity:
        return block(f"Limite de perte journalière atteinte ({realized_today:,.0f} $, limite "
                     f"{settings.daily_loss_limit_pct:.1f} %) : plus de nouvelle prise de "
                     "risque aujourd'hui.")
    used_risk = sum(o.risk for o in open_positions)
    room = settings.max_open_risk_pct / 100 * equity - used_risk
    if room <= 0:
        return block(f"Risque cumulé des positions ouvertes déjà au maximum "
                     f"({used_risk / equity * 100:.1f} % pour {settings.max_open_risk_pct:.1f} %).")
    budget = min(settings.risk_pct / 100 * equity, room)
    if budget < settings.risk_pct / 100 * equity:
        out.warnings.append(f"Risque réduit à {budget / equity * 100:.2f} % pour respecter le "
                            f"risque cumulé maximum de {settings.max_open_risk_pct:.1f} %.")

    # Perte par unité si le stop est touché : distance + spread + glissement + commission.
    per_unit = (abs(entry - stop) + p.spread + p.slippage
                + p.commission_per_lot / p.contract_size + p.commission_pct / 100 * entry)
    units = budget / per_unit

    def to_lots(u: float) -> float:
        steps = math.floor(u / p.contract_size / p.lot_step + 1e-9)
        return round(steps * p.lot_step, 8)

    lots = to_lots(units)
    # Levier : nominal total (positions existantes comprises) / fonds propres.
    open_notional = sum(o.units * o.entry for o in open_positions)
    max_notional = settings.max_leverage * equity - open_notional
    if lots * p.contract_size * entry > max_notional:
        lots = to_lots(max(max_notional, 0) / entry)
        out.warnings.append(f"Taille réduite pour respecter le levier maximum de "
                            f"×{settings.max_leverage:.0f}.")
    # Marge disponible.
    free_margin = equity - sum(o.margin for o in open_positions)
    margin_per_lot = p.contract_size * entry * p.margin_pct / 100
    if lots * margin_per_lot > free_margin:
        lots = to_lots(max(free_margin, 0) / margin_per_lot * p.contract_size)
        out.warnings.append("Taille réduite : marge disponible insuffisante.")
    if lots < p.min_lot - 1e-9:
        return block(f"La taille calculée est inférieure au lot minimum ({p.min_lot}) : stop "
                     "trop éloigné pour le risque autorisé, ou limites atteintes.")

    out.lots = lots
    out.units = lots * p.contract_size
    out.notional = out.units * entry
    out.margin = lots * margin_per_lot
    out.leverage = (open_notional + out.notional) / equity
    out.costs = round_trip_costs(p, out.units, entry, slipped=True)
    out.risk_at_stop = abs(entry - stop) * out.units + out.costs
    if target is not None:
        reward = abs(target - entry) * out.units
        risk = abs(entry - stop) * out.units
        out.gross_rr = reward / risk if risk else None
        net_reward = reward - round_trip_costs(p, out.units, entry, slipped=False)
        out.net_rr = net_reward / out.risk_at_stop if out.risk_at_stop else None
    return out
