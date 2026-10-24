"""Orchestration : données → 4 spécialistes en parallèle → décision du chef de desk."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

import anthropic
import pandas as pd

from . import agents, config, data, economic_calendar, indicators, smc
from .config import ASSETS, TRADING_STYLES, Asset


@dataclass
class Context:
    asset: Asset
    style: str
    frames: dict[str, pd.DataFrame]
    price: float
    technical: str
    macro: str
    sentiment: str = ""
    source: str = ""
    notes: str = ""
    calendar: str = ""
    style_label: str = ""
    smc_map: Optional[smc.SMCMap] = None

    @property
    def smc_text(self) -> str:
        return smc.format_map(self.smc_map) if self.smc_map else ""

    @property
    def agent_specs(self) -> tuple[agents.AgentSpec, ...]:
        return agents.SPECIALISTS + ((agents.SMC,) if self.smc_map else ())

    def briefing(self, agent_key: str) -> str:
        now = datetime.now(timezone.utc).strftime("%A %d %B %Y, %H:%M UTC")
        head = (
            f"Date et heure : {now}\n"
            f"Actif : {self.asset.name}\n"
            f"Contexte : {self.asset.description}\n"
            f"Prix actuel : {self.price:.2f} (source : {self.source or 'n/d'})\n"
            f"Style de trading du desk : {self.style}\n"
        )
        if self.notes.strip():
            head += f"Remarques du trader : {self.notes.strip()}\n"

        if agent_key == "technique":
            return (
                head
                + "\n## Indicateurs calculés\n\n"
                + self.technical
                + "\n\n## Dernières bougies 4h\n\n"
                + indicators.recent_candles(self.frames["4h"])
                + "\n\nRédige ton analyse technique."
            )
        if agent_key == "fondamental":
            return (
                head
                + "\n## Tableau inter-marchés (clôtures journalières)\n\n"
                + self.macro
                + "\n\n## Publications américaines à venir (heures UTC)\n\n"
                + (self.calendar or "non disponible")
                + "\n\nRédige ton analyse fondamentale et macro."
            )
        if agent_key == "smc":
            return (
                head
                + "\n## Cartographie Smart Money calculée\n\n"
                + self.smc_text
                + "\n\nRédige ton analyse SMC."
            )
        if agent_key == "sentiment":
            return (
                head
                + "\n## Données de sentiment disponibles\n\n"
                + self.sentiment
                + "\n\nRédige ton analyse du sentiment et du positionnement."
            )
        return head + "\nRédige ton analyse géopolitique et d'actualité."


def smc_for(frames: dict[str, pd.DataFrame], style_label: str) -> smc.SMCMap:
    tf, htf = smc.STYLE_TIMEFRAMES[style_label]
    return smc.analyze(frames[tf], tf, frames.get(htf), htf)


def prepare(asset_key: str, style_label: str, notes: str = "", smc_mode: bool = False) -> Context:
    asset = ASSETS[asset_key]
    prices = data.get_price_history(asset, with_15m=smc_mode)
    frames = prices.frames
    return Context(
        asset=asset,
        style=TRADING_STYLES[style_label],
        style_label=style_label,
        smc_map=smc_for(frames, style_label) if smc_mode else None,
        calendar=economic_calendar.safe_calendar_text(),
        frames=frames,
        price=float(frames["1h"]["Close"].iloc[-1]),
        technical=indicators.format_technical(frames),
        macro=data.format_macro(data.macro_snapshot()),
        sentiment=data.sentiment_snapshot(asset, frames),
        source=prices.source,
        notes=notes,
    )


def run_specialists(
    client: anthropic.Anthropic, ctx: Context, model: str = config.MODEL
) -> Iterator[agents.AgentReport]:
    """Lance les analystes en parallèle et renvoie chaque rapport dès qu'il arrive."""
    specs = ctx.agent_specs
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [
            pool.submit(agents.run_specialist, client, spec, ctx.briefing(spec.key), model)
            for spec in specs
        ]
        for fut in as_completed(futures):
            yield fut.result()


def decide(
    client: anthropic.Anthropic,
    ctx: Context,
    reports: list[agents.AgentReport],
    model: str = config.MODEL,
    calendar_forecast: str = "",
) -> agents.TradingDecision:
    order = {s.key: i for i, s in enumerate(agents.DECISION_AGENTS)}
    reports = sorted(reports, key=lambda r: order.get(r.key, 99))
    prompt = agents.build_decision_prompt(
        ctx.asset.name, ctx.price, ctx.style, reports, ctx.notes
    ) + "\n\n## Rappel des niveaux techniques\n\n" + ctx.technical
    if ctx.calendar:
        prompt += "\n\n## Publications américaines à venir (heures UTC)\n\n" + ctx.calendar
    if calendar_forecast:
        prompt += "\n\n## Prévisions de l'économiste pour ces publications\n\n" + calendar_forecast
    if ctx.smc_map:
        prompt += "\n" + agents.SMC_RULE + "\n## Setup SMC calculé\n\n" + ctx.smc_text
    decision = agents.run_decision(client, prompt, model)
    if ctx.smc_map:
        decision, _ = enforce_inducement_rule(decision, ctx.smc_map.setup)
    return decision


def enforce_inducement_rule(
    decision: agents.TradingDecision, setup: smc.Setup
) -> tuple[agents.TradingDecision, Optional[str]]:
    """Mode SMC : pas d'entrée tant que l'inducement n'est pas balayé.

    Une décision ACHAT / VENTE qui ne respecte pas la règle devient ATTENDRE ; les
    niveaux proposés sont conservés comme plan conditionnel.
    """
    if decision.decision == "ATTENDRE":
        return decision, None
    expected = {"haussier": "ACHAT", "baissier": "VENTE"}.get(setup.direction or "")
    if setup.entry_allowed and decision.decision == expected:
        return decision, None
    if not setup.entry_allowed:
        level = f" à {setup.idm_level:.2f}" if setup.idm_level is not None else ""
        reason = (f"Entrée bloquée par la règle SMC : l'inducement{level} n'est pas encore "
                  f"balayé (statut : {setup.status}).")
    else:
        reason = (f"Entrée bloquée par la règle SMC : un {decision.decision} va contre le "
                  f"setup {setup.direction} validé par l'inducement.")
    updated = decision.model_copy(update={
        "decision": "ATTENDRE",
        "synthese": f"⏳ {reason} Le plan initial ({decision.decision}) est conservé comme "
                    f"ordre conditionnel.\n\n{decision.synthese}",
        "zone_entree": f"Conditionnel — {decision.zone_entree}",
    })
    return updated, reason


def position_size(
    capital: float, risk_pct: float, entry: Optional[float], stop: Optional[float]
) -> Optional[dict]:
    """Taille de position pour ne risquer que `risk_pct` % du capital si le stop est touché."""
    if not entry or not stop or entry == stop or capital <= 0 or risk_pct <= 0:
        return None
    risk_amount = capital * risk_pct / 100
    units = risk_amount / abs(entry - stop)
    return {
        "montant_risque": risk_amount,
        "unites": units,
        "valeur_position": units * entry,
        "levier_implicite": units * entry / capital,
    }


def risk_reward(entry: Optional[float], stop: Optional[float], target: Optional[float]):
    if not entry or not stop or not target or entry == stop:
        return None
    return abs(target - entry) / abs(entry - stop)
