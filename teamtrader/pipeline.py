"""Orchestration : données → 4 spécialistes en parallèle → décision du chef de desk."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

import anthropic
import pandas as pd

from . import (agents, config, costs, data, economic_calendar, indicators, quality, replay,
               smc, validation)
from .config import ASSETS, TRADING_STYLES, Asset

log = logging.getLogger("teamtrader.pipeline")


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
    smc_mode: bool = False
    portfolio: str = ""
    provider: str = "yahoo"
    basis: float = 0.0
    data_items: list[quality.DataItem] = field(default_factory=list)

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
        if self.portfolio:
            head += f"Positions du trader (paper trading) :\n{self.portfolio}\n"
        if self.notes.strip():
            head += f"Remarques du trader : {self.notes.strip()}\n"
        degraded = [i for i in self.data_items if i.status != quality.OK]
        if degraded:
            head += "Données manquantes ou périmées : " + ", ".join(
                f"{i.name} ({i.status})" for i in degraded) + "\n"

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


def smc_for(frames: dict[str, pd.DataFrame], style_label: str) -> Optional[smc.SMCMap]:
    tf, htf = smc.STYLE_TIMEFRAMES[style_label]
    if tf not in frames or len(frames[tf]) < 50:
        return None
    return smc.analyze(frames[tf], tf, frames.get(htf), htf)


def prepare(asset_key: str, style_label: str, notes: str = "", smc_mode: bool = False,
            portfolio: str = "") -> Context:
    asset = ASSETS[asset_key]
    now = datetime.now(timezone.utc)
    prices = data.get_price_history(asset, with_15m=smc_mode)
    frames = prices.frames
    items = [quality.price_item(asset_key, frames["1h"], prices.source, now)]
    smc_map = None
    if smc_mode:
        tf, _ = smc.STYLE_TIMEFRAMES[style_label]
        items.append(quality.price_item(asset_key, frames.get(tf), prices.source, now,
                                        replay.TF_MINUTES[tf], f"Prix (bougies {tf}, SMC)"))
        smc_map = smc_for(frames, style_label)
    macro_rows = data.macro_snapshot()
    items.append(data.macro_item(macro_rows, now))
    sentiment, sentiment_items = data.sentiment_data(asset, frames, now)
    items += sentiment_items
    calendar, calendar_item = economic_calendar.calendar_with_status()
    items.append(calendar_item)
    return Context(
        asset=asset,
        style=TRADING_STYLES[style_label],
        style_label=style_label,
        smc_map=smc_map,
        smc_mode=smc_mode,
        calendar=calendar,
        frames=frames,
        price=float(frames["1h"]["Close"].iloc[-1]),
        technical=indicators.format_technical(frames),
        macro=data.format_macro(macro_rows),
        sentiment=sentiment,
        source=prices.source,
        provider=prices.provider,
        basis=prices.basis,
        notes=notes,
        portfolio=portfolio,
        data_items=items,
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


@dataclass
class DecisionResult:
    decision: agents.TradingDecision
    # Erreurs de la première proposition du chef de desk (corrigées ou non).
    validation_errors: list[str] = field(default_factory=list)
    retried: bool = False
    rejected: bool = False
    # Raisons ayant forcé ATTENDRE (règle SMC, qualité des données…).
    blocked_by: list[str] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    model: str = ""
    quality: Optional[quality.QualityReport] = None

    @property
    def status(self) -> str:
        if self.rejected:
            return "rejetée"
        if self.blocked_by:
            return "bloquée"
        return "corrigée" if self.retried else "valide"


def decision_prompt(ctx: Context, reports: list[agents.AgentReport],
                    calendar_forecast: str = "") -> str:
    order = {s.key: i for i, s in enumerate(agents.DECISION_AGENTS)}
    reports = sorted(reports, key=lambda r: order.get(r.key, 99))
    prompt = agents.build_decision_prompt(
        ctx.asset.name, ctx.price, ctx.style, reports, ctx.notes
    ) + "\n\n## Rappel des niveaux techniques\n\n" + ctx.technical
    if ctx.calendar:
        prompt += "\n\n## Publications américaines à venir (heures UTC)\n\n" + ctx.calendar
    if calendar_forecast:
        prompt += "\n\n## Prévisions de l'économiste pour ces publications\n\n" + calendar_forecast
    if ctx.portfolio:
        prompt += "\n\n## Positions du trader (paper trading)\n\n" + ctx.portfolio
    if ctx.smc_map:
        prompt += "\n" + agents.SMC_RULE + "\n## Setup SMC calculé\n\n" + ctx.smc_text
    prompt += (f"\n\n## Contrôles automatiques\nUne prise de position doit avoir une entrée, un "
               f"stop et des objectifs cohérents, et un ratio risque / rendement d'au moins "
               f"{validation.MIN_RISK_REWARD:.1f} sur l'objectif 1, sinon elle sera rejetée.")
    return prompt


def decide(
    client: anthropic.Anthropic,
    ctx: Context,
    reports: list[agents.AgentReport],
    model: str = config.MODEL,
    calendar_forecast: str = "",
) -> DecisionResult:
    # 0. Seuil de qualité : sans données ou rapports essentiels, pas de décision (ni d'appel).
    q = quality.assess(ctx.data_items, reports, ctx.smc_mode)
    if not q.ok:
        log.warning("Décision bloquée par le contrôle de qualité : %s", q.blocking)
        return DecisionResult(wait_decision(q.blocking), blocked_by=list(q.blocking), quality=q)

    prompt = decision_prompt(ctx, reports, calendar_forecast)
    decision, usage = agents.run_decision(client, prompt, model)
    result = DecisionResult(decision, usage=[usage], model=usage.get("model", ""), quality=q)

    # 1. Contrôles déterministes ; une seule relance corrective.
    check = validation.validate_decision(decision, ctx.price)
    if not check.ok:
        result.validation_errors = check.errors
        result.retried = True
        log.warning("Décision invalide, relance corrective : %s", check.errors)
        fix = (prompt + "\n\n## Ta proposition précédente a été REJETÉE par les contrôles\n"
               + "\n".join(f"- {e}" for e in check.errors)
               + f"\n\nProposition rejetée : {decision.model_dump_json()}\n\n"
               "Corrige les niveaux, ou choisis ATTENDRE si aucun trade cohérent n'existe.")
        decision, usage = agents.run_decision(client, fix, model)
        result.usage.append(usage)
        second = validation.validate_decision(decision, ctx.price)
        if not second.ok:
            decision = validation.rejected(decision, second.errors)
            result.rejected = True

    # 2. Règle d'inducement en mode SMC.
    if ctx.smc_map:
        decision, reason = enforce_inducement_rule(decision, ctx.smc_map.setup)
        if reason:
            result.blocked_by.append(reason)
    result.decision = decision
    return result


def wait_decision(reasons: list[str]) -> agents.TradingDecision:
    """Décision ATTENDRE produite par le logiciel, sans appel au chef de desk."""
    return agents.TradingDecision(
        decision="ATTENDRE", confiance=0, horizon="—",
        synthese="⛔ Qualité insuffisante pour décider : " + " ; ".join(reasons),
        prix_entree=None, zone_entree="Aucune", stop_loss=None, take_profits=[],
        vues_agents=[], arguments_pour=[], arguments_contre=list(reasons),
        conditions_invalidation=[], evenements_a_surveiller=[],
    )


def analysis_record(ctx: Context, reports: list[agents.AgentReport], result: DecisionResult,
                    duration_s: Optional[float] = None) -> dict:
    """Tout ce qu'il faut pour rouvrir et comprendre une analyse plus tard."""
    usages = [u for r in reports for u in r.usage] + result.usage
    return {
        "parametres": {
            "modele_demande": config.MODEL,
            "effort_specialistes": config.SPECIALIST_EFFORT,
            "effort_decision": config.DECISION_EFFORT,
            "recherches_web_max": config.WEB_SEARCH_MAX_USES,
            "rr_minimum": validation.MIN_RISK_REWARD,
            "methode_evaluation": replay.METHOD_VERSION,
            "style": ctx.style_label, "mode_smc": ctx.smc_mode,
        },
        "source_prix": {"description": ctx.source, "fournisseur": ctx.provider,
                        "ecart_future_spot": ctx.basis, "prix": ctx.price},
        "qualite": result.quality.as_dict() if result.quality else None,
        "decision": {"statut": result.status, "erreurs_validation": result.validation_errors,
                     "bloquee_par": result.blocked_by, "modele": result.model},
        "rapports": [
            {"agent": r.key, "nom": r.name, "direction": r.direction, "confiance": r.confidence,
             "erreur": r.error, "modele": r.model, "duree_s": r.duration_s, "texte": r.text,
             "sources": r.sources}
            for r in reports
        ],
        "setup_smc": ctx.smc_text or None,
        "portefeuille": ctx.portfolio or None,
        "couts": costs.total(usages),
        "consommation": usages,
        "duree_s": duration_s,
    }


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


risk_reward = validation.risk_reward
