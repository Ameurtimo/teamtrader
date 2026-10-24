"""Orchestration : données → 4 spécialistes en parallèle → décision du chef de desk."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

import anthropic
import pandas as pd

from . import agents, config, data, indicators
from .config import ASSETS, Asset


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
                + "\n\nRédige ton analyse fondamentale et macro."
            )
        if agent_key == "sentiment":
            return (
                head
                + "\n## Données de sentiment disponibles\n\n"
                + self.sentiment
                + "\n\nRédige ton analyse du sentiment et du positionnement."
            )
        return head + "\nRédige ton analyse géopolitique et d'actualité."


def prepare(asset_key: str, style: str, notes: str = "") -> Context:
    asset = ASSETS[asset_key]
    prices = data.get_price_history(asset)
    frames = prices.frames
    return Context(
        asset=asset,
        style=style,
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
    with ThreadPoolExecutor(max_workers=len(agents.SPECIALISTS)) as pool:
        futures = [
            pool.submit(agents.run_specialist, client, spec, ctx.briefing(spec.key), model)
            for spec in agents.SPECIALISTS
        ]
        for fut in as_completed(futures):
            yield fut.result()


def decide(
    client: anthropic.Anthropic,
    ctx: Context,
    reports: list[agents.AgentReport],
    model: str = config.MODEL,
) -> agents.TradingDecision:
    order = {s.key: i for i, s in enumerate(agents.SPECIALISTS)}
    reports = sorted(reports, key=lambda r: order[r.key])
    prompt = agents.build_decision_prompt(
        ctx.asset.name, ctx.price, ctx.style, reports, ctx.notes
    ) + "\n\n## Rappel des niveaux techniques\n\n" + ctx.technical
    return agents.run_decision(client, prompt, model)


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
