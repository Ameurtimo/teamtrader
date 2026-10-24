"""Smart Money Concepts : détection locale et déterministe.

On calcule ici, à partir des bougies, tout ce que l'agent SMC va interpréter :
- les swings (plus hauts / plus bas confirmés),
- la structure : BOS (cassure dans le sens de la tendance) et CHoCH (changement
  de caractère, cassure à contre-tendance), sur clôture,
- les order blocks (dernière bougie opposée avant l'impulsion qui casse la structure),
- les fair value gaps (déséquilibres de 3 bougies),
- la liquidité (égaux hauts / égaux bas non balayés),
- la zone premium / discount de la jambe en cours,
- l'inducement (IDM) : premier repli interne après le BOS. Règle du desk : une
  entrée n'est VALIDE qu'une fois cet inducement balayé par le prix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd

from .indicators import atr

Direction = Literal["haussier", "baissier"]

# Unité de temps du setup et unité de temps supérieure (biais) selon le style.
STYLE_TIMEFRAMES = {
    "Intraday (quelques heures)": ("15m", "1h"),
    "Swing (quelques jours)": ("1h", "4h"),
    "Position (quelques semaines)": ("4h", "1d"),
}

MAJOR_WINDOW = 5   # bougies de chaque côté pour un swing de structure
MINOR_WINDOW = 2   # bougies de chaque côté pour un swing interne (inducement)


@dataclass
class Swing:
    idx: int
    ts: pd.Timestamp
    price: float
    kind: Literal["high", "low"]


@dataclass
class StructureBreak:
    idx: int
    ts: pd.Timestamp
    level: float
    direction: Direction
    kind: Literal["BOS", "CHoCH"]
    swing_idx: int
    swing_ts: pd.Timestamp


@dataclass
class Zone:
    kind: Literal["OB", "FVG"]
    direction: Direction
    top: float
    bottom: float
    idx: int
    ts: pd.Timestamp
    state: str = "intact"  # intact | touché | invalidé (OB) ; ouvert | partiel | comblé (FVG)

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Liquidity:
    kind: Literal["égaux hauts", "égaux bas"]
    price: float
    count: int
    ts: pd.Timestamp


@dataclass
class Setup:
    direction: Optional[Direction]
    status: str
    entry_allowed: bool
    idm_level: Optional[float] = None
    idm_ts: Optional[pd.Timestamp] = None
    idm_swept: bool = False
    idm_swept_ts: Optional[pd.Timestamp] = None
    leg_start: Optional[float] = None
    leg_end: Optional[float] = None
    equilibrium: Optional[float] = None
    poi: Optional[Zone] = None
    poi_in_discount: Optional[bool] = None
    poi_touched: bool = False
    entry: Optional[float] = None
    stop: Optional[float] = None
    targets: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SMCMap:
    timeframe: str
    price: float
    atr: float
    trend: Optional[Direction]
    breaks: list[StructureBreak]
    order_blocks: list[Zone]
    fvgs: list[Zone]
    liquidity: list[Liquidity]
    setup: Setup
    htf_timeframe: Optional[str] = None
    htf_trend: Optional[Direction] = None


# ------------------------------------------------------------------ briques


def find_swings(df: pd.DataFrame, window: int) -> list[Swing]:
    """Swings confirmés : il faut `window` bougies de chaque côté."""
    highs, lows = df["High"].to_numpy(), df["Low"].to_numpy()
    out: list[Swing] = []
    for i in range(window, len(df) - window):
        lo, hi = i - window, i + window + 1
        if highs[i] == highs[lo:hi].max() and highs[i] > highs[lo:i].max():
            out.append(Swing(i, df.index[i], float(highs[i]), "high"))
        if lows[i] == lows[lo:hi].min() and lows[i] < lows[lo:i].min():
            out.append(Swing(i, df.index[i], float(lows[i]), "low"))
    return out


def market_structure(
    df: pd.DataFrame, swings: list[Swing], window: int
) -> tuple[Optional[Direction], list[StructureBreak]]:
    """Parcourt les bougies et enregistre chaque cassure (sur clôture) du dernier swing."""
    close = df["Close"].to_numpy()
    pending = sorted(swings, key=lambda s: s.idx + window)  # ordre de confirmation
    ref_high: Optional[Swing] = None
    ref_low: Optional[Swing] = None
    trend: Optional[Direction] = None
    breaks: list[StructureBreak] = []
    p = 0
    for i in range(len(df)):
        while p < len(pending) and pending[p].idx + window <= i:
            s = pending[p]
            if s.kind == "high":
                ref_high = s
            else:
                ref_low = s
            p += 1
        if ref_high is not None and close[i] > ref_high.price:
            kind = "BOS" if trend in (None, "haussier") else "CHoCH"
            breaks.append(StructureBreak(i, df.index[i], ref_high.price, "haussier", kind,
                                         ref_high.idx, ref_high.ts))
            trend, ref_high = "haussier", None
        elif ref_low is not None and close[i] < ref_low.price:
            kind = "BOS" if trend in (None, "baissier") else "CHoCH"
            breaks.append(StructureBreak(i, df.index[i], ref_low.price, "baissier", kind,
                                         ref_low.idx, ref_low.ts))
            trend, ref_low = "baissier", None
    return trend, breaks


def _leg_origin(df: pd.DataFrame, brk: StructureBreak) -> int:
    """Indice de l'extrême (plus bas pour une cassure haussière) qui a lancé l'impulsion."""
    seg = df.iloc[brk.swing_idx : brk.idx + 1]
    col = "Low" if brk.direction == "haussier" else "High"
    pos = seg[col].to_numpy().argmin() if brk.direction == "haussier" else seg[col].to_numpy().argmax()
    return brk.swing_idx + int(pos)


def order_block_for(df: pd.DataFrame, brk: StructureBreak) -> Optional[Zone]:
    """Dernière bougie opposée avant l'impulsion qui a cassé la structure."""
    origin = _leg_origin(df, brk)
    o, c = df["Open"].to_numpy(), df["Close"].to_numpy()
    for j in range(origin, max(brk.swing_idx, origin - 10) - 1, -1):
        bearish_candle = c[j] < o[j]
        if (brk.direction == "haussier") == bearish_candle:
            return Zone("OB", brk.direction, float(df["High"].iloc[j]),
                        float(df["Low"].iloc[j]), j, df.index[j])
    j = origin  # à défaut, la bougie extrême elle-même
    return Zone("OB", brk.direction, float(df["High"].iloc[j]), float(df["Low"].iloc[j]),
                j, df.index[j])


def update_zone_state(df: pd.DataFrame, z: Zone, after_idx: int) -> Zone:
    later = df.iloc[after_idx + 1 :]
    if later.empty:
        return z
    if z.kind == "OB":
        if z.direction == "haussier":
            if (later["Close"] < z.bottom).any():
                z.state = "invalidé"
            elif (later["Low"] <= z.top).any():
                z.state = "touché"
        else:
            if (later["Close"] > z.top).any():
                z.state = "invalidé"
            elif (later["High"] >= z.bottom).any():
                z.state = "touché"
    else:
        if z.direction == "haussier":
            if (later["Low"] <= z.bottom).any():
                z.state = "comblé"
            elif (later["Low"] < z.top).any():
                z.state = "partiel"
        else:
            if (later["High"] >= z.top).any():
                z.state = "comblé"
            elif (later["High"] > z.bottom).any():
                z.state = "partiel"
    return z


def find_fvgs(df: pd.DataFrame, min_size: float) -> list[Zone]:
    hi, lo = df["High"].to_numpy(), df["Low"].to_numpy()
    out = []
    for i in range(2, len(df)):
        if lo[i] > hi[i - 2] and lo[i] - hi[i - 2] >= min_size:
            z = Zone("FVG", "haussier", float(lo[i]), float(hi[i - 2]), i - 1, df.index[i - 1], "ouvert")
            out.append(update_zone_state(df, z, i))
        elif hi[i] < lo[i - 2] and lo[i - 2] - hi[i] >= min_size:
            z = Zone("FVG", "baissier", float(lo[i - 2]), float(hi[i]), i - 1, df.index[i - 1], "ouvert")
            out.append(update_zone_state(df, z, i))
    return out


def find_liquidity(df: pd.DataFrame, swings: list[Swing], tol: float) -> list[Liquidity]:
    """Égaux hauts / égaux bas encore non balayés (stops accumulés au-dessus / en dessous)."""
    out = []
    for kind, label in (("high", "égaux hauts"), ("low", "égaux bas")):
        pts = [s for s in swings if s.kind == kind][-12:]
        used: set[int] = set()
        for a in range(len(pts)):
            if a in used:
                continue
            group = [pts[a]] + [pts[b] for b in range(a + 1, len(pts))
                                if b not in used and abs(pts[b].price - pts[a].price) <= tol]
            if len(group) < 2:
                continue
            used.update(pts.index(g) for g in group)
            last = max(g.idx for g in group)
            later = df.iloc[last + 1 :]
            level = max(g.price for g in group) if kind == "high" else min(g.price for g in group)
            swept = (later["High"] > level).any() if kind == "high" else (later["Low"] < level).any()
            if not swept:
                out.append(Liquidity(label, level, len(group), df.index[last]))
    return out


# ------------------------------------------------------------------ setup avec inducement


def find_setup(
    df: pd.DataFrame, breaks: list[StructureBreak], zones: list[Zone],
    liquidity: list[Liquidity], atr_value: float,
) -> Setup:
    if not breaks:
        return Setup(None, "pas de structure claire", False,
                     notes=["Aucune cassure de structure détectée sur la période."])
    brk = breaks[-1]
    bull = brk.direction == "haussier"
    origin = _leg_origin(df, brk)
    leg_start = float(df["Low"].iloc[origin] if bull else df["High"].iloc[origin])
    after = df.iloc[brk.idx :]
    ext_pos = int(after["High"].to_numpy().argmax() if bull else after["Low"].to_numpy().argmin())
    ext_idx = brk.idx + ext_pos
    leg_end = float(df["High"].iloc[ext_idx] if bull else df["Low"].iloc[ext_idx])
    eq = (leg_start + leg_end) / 2
    setup = Setup(brk.direction, "", False, leg_start=leg_start, leg_end=leg_end, equilibrium=eq)
    side = "au-dessus" if bull else "en dessous"
    setup.notes.append(f"Dernière cassure : {brk.kind} {brk.direction}, clôture {side} de "
                       f"{brk.level:.2f} ({brk.ts:%Y-%m-%d %H:%M} UTC).")

    # Structure invalidée : le prix a clôturé au-delà de l'origine de la jambe.
    closes_after = df["Close"].iloc[brk.idx :]
    if (closes_after < leg_start).any() if bull else (closes_after > leg_start).any():
        setup.status = "invalidé (origine de la jambe cassée)"
        return setup

    # Inducement : dernier repli interne formé entre le BOS et l'extrême de la jambe.
    minor = [s for s in find_swings(df, MINOR_WINDOW)
             if brk.idx <= s.idx < ext_idx and s.kind == ("low" if bull else "high")]
    if not minor:
        setup.status = "en attente : inducement pas encore formé"
        setup.notes.append("Aucun repli interne depuis la cassure : on attend qu'un premier "
                           "pullback crée l'inducement.")
        return setup
    idm = minor[-1]
    setup.idm_level, setup.idm_ts = idm.price, idm.ts
    later = df.iloc[ext_idx + 1 :]
    swept_mask = (later["Low"] < idm.price) if bull else (later["High"] > idm.price)
    if swept_mask.any():
        setup.idm_swept = True
        setup.idm_swept_ts = later.index[int(swept_mask.to_numpy().argmax())]

    # POI : order block ou FVG dans le sens du setup, situé au-delà de l'inducement,
    # dans la jambe, et pas invalidé. On préfère les zones en discount (premium pour une vente).
    def beyond_idm(z: Zone) -> bool:
        return z.top <= idm.price if bull else z.bottom >= idm.price

    def in_leg(z: Zone) -> bool:
        return z.bottom >= leg_start - atr_value if bull else z.top <= leg_start + atr_value

    cands = [z for z in zones if z.direction == brk.direction and z.state not in
             ("invalidé", "comblé") and beyond_idm(z) and in_leg(z) and z.idx >= brk.swing_idx]
    discount = [z for z in cands if (z.top <= eq if bull else z.bottom >= eq)]
    pool = discount or cands
    # Un order block est un POI plus fiable qu'un simple FVG : on le préfère s'il existe.
    pool = [z for z in pool if z.kind == "OB"] or pool
    if pool:
        # Le POI le plus proche de l'inducement (le premier que le prix rencontrera).
        poi = max(pool, key=lambda z: z.top) if bull else min(pool, key=lambda z: z.bottom)
        setup.poi_in_discount = bool(discount)
    else:
        poi = update_zone_state(df, order_block_for(df, brk), brk.idx)
        if poi.state == "invalidé":
            setup.status = "invalidé (order block d'origine cassé)"
            return setup
        setup.poi_in_discount = (poi.top <= eq) if bull else (poi.bottom >= eq)
        setup.notes.append("Pas de POI intermédiaire : on retient l'order block d'origine.")
    setup.poi = poi
    tail = df.iloc[ext_idx + 1 :]
    setup.poi_touched = bool((tail["Low"] <= poi.top).any() if bull else (tail["High"] >= poi.bottom).any())

    # Stop au-delà du POI, à au moins 0,5 ATR de l'entrée pour ne pas sortir sur du bruit.
    buffer, min_risk = 0.1 * atr_value, 0.5 * atr_value
    setup.entry = poi.top if bull else poi.bottom
    if bull:
        setup.stop = min(poi.bottom - buffer, setup.entry - min_risk)
    else:
        setup.stop = max(poi.top + buffer, setup.entry + min_risk)
    setup.targets = [leg_end]
    # Objectif 2 : la liquidité suivante DANS LE SENS du trade (égaux hauts au-dessus de la
    # jambe pour un achat, égaux bas en dessous pour une vente).
    if bull:
        pools = [l.price for l in liquidity if l.kind == "égaux hauts" and l.price > leg_end]
    else:
        pools = [l.price for l in liquidity if l.kind == "égaux bas" and l.price < leg_end]
    if pools:
        setup.targets.append(min(pools) if bull else max(pools))

    if not setup.idm_swept:
        setup.status = "en attente : inducement non balayé"
        setup.notes.append(
            f"Entrée NON valide tant que le prix n'a pas balayé l'inducement à {idm.price:.2f} "
            f"({'passer sous' if bull else 'passer au-dessus de'} ce niveau)."
        )
    else:
        setup.entry_allowed = True
        setup.status = ("validé : inducement balayé, prix dans le POI" if setup.poi_touched
                        else "validé : inducement balayé, ordre limite sur le POI")
    return setup


def analyze(df: pd.DataFrame, timeframe: str, htf: Optional[pd.DataFrame] = None,
            htf_timeframe: Optional[str] = None) -> SMCMap:
    df = df.tail(600)
    a = float(atr(df).iloc[-1])
    swings = find_swings(df, MAJOR_WINDOW)
    trend, breaks = market_structure(df, swings, MAJOR_WINDOW)
    obs = []
    for b in breaks[-8:]:
        z = order_block_for(df, b)
        if z is not None:
            obs.append(update_zone_state(df, z, b.idx))
    fvgs = [z for z in find_fvgs(df, 0.15 * a) if z.state != "comblé"][-8:]
    liq = find_liquidity(df, swings, 0.15 * a)
    setup = find_setup(df, breaks, obs + fvgs, liq, a)

    htf_trend = None
    if htf is not None and len(htf) > 30:
        htf = htf.tail(600)
        htf_trend, _ = market_structure(htf, find_swings(htf, MAJOR_WINDOW), MAJOR_WINDOW)
        if setup.direction and htf_trend and htf_trend != setup.direction:
            setup.notes.append(f"⚠️ Setup {setup.direction} contre la tendance {htf_timeframe} "
                               f"({htf_trend}) : probabilité plus faible.")
    return SMCMap(timeframe, float(df["Close"].iloc[-1]), a, trend, breaks, obs, fvgs, liq,
                  setup, htf_timeframe, htf_trend)


# ------------------------------------------------------------------ texte pour l'agent


def _z(z: Zone) -> str:
    return f"{z.kind} {z.direction} {z.bottom:.2f}–{z.top:.2f} ({z.ts:%m-%d %H:%M}, {z.state})"


def format_map(m: SMCMap) -> str:
    s = m.setup
    lines = [
        f"Unité de temps du setup : {m.timeframe} — prix {m.price:.2f} — ATR14 {m.atr:.2f}",
        f"Tendance structurelle {m.timeframe} : {m.trend or 'indéterminée'}"
        + (f" | tendance {m.htf_timeframe} : {m.htf_trend or 'indéterminée'}" if m.htf_timeframe else ""),
        "",
        "Dernières cassures de structure :",
        *[f"- {b.kind} {b.direction} : clôture au-delà de {b.level:.2f} le {b.ts:%Y-%m-%d %H:%M}"
          for b in m.breaks[-5:]],
        "",
        "Order blocks récents :", *[f"- {_z(z)}" for z in m.order_blocks[-5:]],
        "",
        "Fair value gaps non comblés :", *([f"- {_z(z)}" for z in m.fvgs[-5:]] or ["- aucun"]),
        "",
        "Liquidité non balayée :",
        *([f"- {l.kind} vers {l.price:.2f} ({l.count} touches)" for l in m.liquidity] or ["- aucune"]),
        "",
        f"SETUP : {s.direction or '—'} — statut : {s.status}",
        f"- Entrée autorisée par la règle d'inducement : {'OUI' if s.entry_allowed else 'NON'}",
    ]
    if s.leg_start is not None:
        lines.append(f"- Jambe : {s.leg_start:.2f} → {s.leg_end:.2f}, équilibre 50 % à {s.equilibrium:.2f}")
    if s.idm_level is not None:
        swept = f"balayé le {s.idm_swept_ts:%Y-%m-%d %H:%M}" if s.idm_swept else "PAS encore balayé"
        lines.append(f"- Inducement : {s.idm_level:.2f} (formé le {s.idm_ts:%Y-%m-%d %H:%M}) — {swept}")
    if s.poi is not None:
        zone = "discount" if s.direction == "haussier" else "premium"
        lines.append(f"- POI : {_z(s.poi)} — en {zone} : {'oui' if s.poi_in_discount else 'non'}"
                     f" — touché : {'oui' if s.poi_touched else 'non'}")
        lines.append(f"- Plan : entrée {s.entry:.2f}, stop {s.stop:.2f}, objectifs "
                     + ", ".join(f"{t:.2f}" for t in s.targets))
    lines += [f"- {n}" for n in s.notes]
    return "\n".join(lines)
