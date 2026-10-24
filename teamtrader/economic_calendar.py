"""Calendrier des publications économiques américaines de la semaine.

Source : le flux hebdomadaire public de Forex Factory (consensus et chiffre précédent).
S'il est indisponible, l'économiste IA retrouve le calendrier par recherche web.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import data, quality

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
IMPACT_FR = {"High": "forte", "Medium": "moyenne", "Low": "faible"}
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def local_time(t: datetime, tz: str) -> str:
    """Date courte en français dans le fuseau demandé, ex. « ven. 06/03 14:30 »."""
    t = t.astimezone(ZoneInfo(tz))
    return f"{JOURS[t.weekday()]} {t:%d/%m %H:%M}"


@dataclass
class EconomicEvent:
    time: datetime  # UTC
    title: str
    impact: str  # forte | moyenne | faible
    forecast: str
    previous: str
    actual: str = ""  # renseigné par le flux une fois le chiffre publié


def parse_events(raw: list[dict], impacts: tuple[str, ...] = ("High", "Medium")) -> list[EconomicEvent]:
    out = []
    for e in raw:
        if e.get("country") != "USD" or e.get("impact") not in impacts:
            continue
        try:
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append(EconomicEvent(
            time=t,
            title=str(e.get("title", "")).strip(),
            impact=IMPACT_FR.get(e["impact"], e["impact"]),
            forecast=str(e.get("forecast") or "").strip() or "n/d",
            previous=str(e.get("previous") or "").strip() or "n/d",
            actual=str(e.get("actual") or "").strip(),
        ))
    return sorted(out, key=lambda ev: ev.time)


def fetch_us_events() -> list[EconomicEvent]:
    return parse_events(data._get_json(CALENDAR_URL))


def upcoming(events: list[EconomicEvent], hours: int = 7 * 24,
             now: Optional[datetime] = None) -> list[EconomicEvent]:
    now = now or datetime.now(timezone.utc)
    return [e for e in events if now - timedelta(hours=1) <= e.time <= now + timedelta(hours=hours)]


def format_events(events: list[EconomicEvent], tz: str = "UTC") -> str:
    if not events:
        return "Aucune publication américaine importante trouvée pour cette période."
    label = "UTC" if tz == "UTC" else tz
    rows = [f"| Date ({label}) | Publication | Impact | Consensus | Précédent |",
            "|---|---|---|---|---|"]
    for e in events:
        rows.append(f"| {local_time(e.time, tz)} | {e.title} | {e.impact} | "
                    f"{e.forecast} | {e.previous} |")
    return "\n".join(rows)


def safe_calendar_text(hours: int = 7 * 24) -> str:
    """Calendrier à venir pour les briefings des agents (jamais d'exception)."""
    try:
        events = upcoming(fetch_us_events(), hours)
    except Exception:
        return "Calendrier économique indisponible : vérifie les publications à venir sur le web."
    return format_events(events)


def calendar_with_status(hours: int = 7 * 24) -> tuple[str, quality.DataItem]:
    """Calendrier à venir + statut de disponibilité pour le contrôle de qualité."""
    try:
        events = upcoming(fetch_us_events(), hours)
    except Exception as e:
        return ("Calendrier économique indisponible : vérifie les publications à venir sur le web.",
                quality.DataItem("Calendrier économique US", "Forex Factory", quality.MISSING,
                                 detail=str(e)[:120]))
    now = datetime.now(timezone.utc)
    return format_events(events), quality.DataItem(
        "Calendrier économique US", "Forex Factory", quality.OK,
        now.isoformat(timespec="minutes"), detail=f"{len(events)} publication(s)")
