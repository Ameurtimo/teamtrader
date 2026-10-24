"""Qualité des données et des rapports : fraîcheur, disponibilité, seuil minimal.

Chaque donnée utilisée par l'analyse reçoit une date (« à jour au »), une source et un
statut : ok, périmée ou indisponible. Avant de demander une décision, on vérifie un
seuil minimal ; s'il n'est pas atteint, la décision est ATTENDRE sans appeler le chef
de desk (pas de coût inutile, pas de décision sur des bases fragiles).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

OK, STALE, MISSING = "ok", "périmée", "indisponible"
# Nombre minimal de rapports exploitables parmi les 4 spécialistes standards.
MIN_STANDARD_REPORTS = 3


@dataclass
class DataItem:
    name: str
    source: str
    status: str
    as_of: Optional[str] = None
    max_age_h: Optional[float] = None
    essential: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def freshness(name: str, source: str, as_of: Optional[datetime], max_age_h: float,
              now: Optional[datetime] = None, essential: bool = False,
              detail: str = "") -> DataItem:
    now = now or datetime.now(timezone.utc)
    if as_of is None:
        return DataItem(name, source, MISSING, None, max_age_h, essential, detail)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    status = OK if now - as_of <= timedelta(hours=max_age_h) else STALE
    return DataItem(name, source, status, as_of.isoformat(timespec="minutes"), max_age_h,
                    essential, detail)


def market_closed(asset_key: str, now: datetime) -> bool:
    """Or : fermé du vendredi ~21 h UTC au dimanche ~22 h UTC."""
    if asset_key != "XAUUSD":
        return False
    wd, h = now.weekday(), now.hour
    return (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 22)


def price_item(asset_key: str, df, source: str, now: Optional[datetime] = None,
               tf_minutes: int = 60, name: str = "Prix (bougies 1 h)") -> DataItem:
    now = now or datetime.now(timezone.utc)
    if df is None or len(df) == 0:
        return DataItem(name, source, MISSING, essential=True)
    last_end = df.index[-1].to_pydatetime() + timedelta(minutes=tf_minutes)
    # Marché fermé (week-end de l'or) : la dernière bougie peut dater de vendredi soir.
    max_age = 60.0 if market_closed(asset_key, now) else 2.0 + tf_minutes / 60
    detail = "marché fermé" if market_closed(asset_key, now) else ""
    return freshness(name, source, last_end, max_age, now, essential=True, detail=detail)


@dataclass
class QualityReport:
    items: list[DataItem] = field(default_factory=list)
    report_status: dict[str, str] = field(default_factory=dict)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def score(self) -> int:
        """Part (en %) des données et rapports disponibles et à jour."""
        checks = [i.status == OK for i in self.items] + \
                 [s == OK for s in self.report_status.values()]
        return round(100 * sum(checks) / len(checks)) if checks else 0

    def as_dict(self) -> dict:
        return {"score": self.score, "ok": self.ok, "blocking": self.blocking,
                "warnings": self.warnings, "items": [i.as_dict() for i in self.items],
                "reports": self.report_status}


def assess(items: list[DataItem], reports, smc_mode: bool) -> QualityReport:
    q = QualityReport(items=list(items))
    for i in items:
        if i.status != OK:
            msg = f"{i.name} : {i.status}" + (f" (à jour au {i.as_of})" if i.as_of else "")
            (q.blocking if i.essential else q.warnings).append(msg)

    standard = ("fondamental", "technique", "geopolitique", "sentiment")
    for r in reports:
        q.report_status[r.key] = OK if r.ok else (r.error or "sans signal exploitable")
    usable = [k for k in standard if q.report_status.get(k) == OK]
    if q.report_status.get("technique") != OK:
        q.blocking.append("Rapport technique indisponible : il est indispensable pour fixer "
                          "l'entrée, le stop et les objectifs.")
    if len(usable) < MIN_STANDARD_REPORTS:
        q.blocking.append(f"Seulement {len(usable)} rapport(s) exploitable(s) sur 4 (minimum "
                          f"{MIN_STANDARD_REPORTS}).")
    if smc_mode and q.report_status.get("smc") != OK:
        q.blocking.append("Mode SMC : le rapport Smart Money est indisponible.")
    for k in standard:
        if k in q.report_status and q.report_status[k] != OK and k != "technique":
            q.warnings.append(f"Rapport {k} : {q.report_status[k]}")
    return q
