"""Les agents de TeamTrader.

Quatre spécialistes analysent chacun une dimension du marché, puis l'agent
général (le « chef de desk ») lit leurs rapports et prend la décision finale
sous forme structurée (validée par Pydantic).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import config

# Si un filtre de sécurité refuse une requête, l'API la relance automatiquement
# sur le modèle de repli recommandé au lieu de renvoyer un refus.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": config.WEB_SEARCH_MAX_USES,
}

SIGNAL_FORMAT = """
Termine TOUJOURS ton rapport par cette section, exactement sous cette forme :

## Signal
- Direction : HAUSSIER | BAISSIER | NEUTRE
- Confiance : <nombre entre 0 et 100>%
- Horizon : <horizon de validité de ton analyse>
"""

COMMON_RULES = """
Tu fais partie d'une équipe de trading composée de quatre analystes spécialisés \
(fondamental, technique, géopolitique, sentiment) et d'un chef de desk qui prend la décision \
finale à partir de vos rapports. Reste dans ton domaine : les autres dimensions \
sont couvertes par tes collègues.

Écris en français, de façon dense et factuelle (300 à 600 mots). Distingue les faits \
(chiffrés et datés quand c'est possible) de tes interprétations. Si une information \
importante manque ou est incertaine, dis-le plutôt que de l'inventer : le chef de desk \
doit savoir à quel point se fier à ton rapport.
"""


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    icon: str
    system: str
    web_search: bool


FUNDAMENTAL = AgentSpec(
    key="fondamental",
    name="Analyste fondamental",
    icon="📊",
    web_search=True,
    system=(
        "Tu es l'analyste fondamental et macroéconomique de l'équipe.\n"
        + COMMON_RULES
        + """
Ton travail : évaluer si l'environnement macro et les flux soutiennent ou pèsent sur \
l'actif. Pour l'or : dollar, taux réels US, anticipations sur la Fed (prochaine \
réunion, probabilités de baisse/hausse), inflation, achats des banques centrales, flux \
ETF, demande physique. Pour le bitcoin : liquidité mondiale, appétit pour le risque, \
flux des ETF spot, régulation, offre (halving, mineurs), indicateurs on-chain, \
corrélation avec le Nasdaq. Le positionnement des traders (COT, funding, ratios \
long/short) est couvert par l'analyste sentiment.

Tu reçois un tableau de données inter-marchés. Utilise la recherche web pour les \
publications récentes (calendrier économique de la semaine, dernières données CPI / \
emploi, discours de la Fed, flux ETF des derniers jours). Cite la date de chaque \
information récente.
"""
        + SIGNAL_FORMAT
    ),
)

TECHNICAL = AgentSpec(
    key="technique",
    name="Analyste technique",
    icon="📈",
    web_search=False,
    system=(
        "Tu es l'analyste technique de l'équipe.\n"
        + COMMON_RULES
        + """
Ton travail : lire la structure du prix sur plusieurs unités de temps (1h, 4h, \
journalier) à partir des indicateurs calculés qu'on te fournit (EMA, RSI, MACD, \
Bollinger, ATR, ADX, supports / résistances, dernières bougies). Identifie la tendance \
dominante, l'alignement ou le conflit entre unités de temps, le momentum, les niveaux \
clés, et un scénario principal et alternatif.

Propose des niveaux concrets : zone d'entrée, stop-loss logique (sous un support ou \
au-delà d'une résistance, cohérent avec l'ATR) et objectifs. Base-toi uniquement sur \
les chiffres fournis : n'invente pas de niveaux qui n'en découlent pas.
"""
        + SIGNAL_FORMAT
    ),
)

GEOPOLITICAL = AgentSpec(
    key="geopolitique",
    name="Analyste géopolitique",
    icon="🌍",
    web_search=True,
    system=(
        "Tu es l'analyste géopolitique et actualité de l'équipe.\n"
        + COMMON_RULES
        + """
Ton travail : recenser les événements géopolitiques et politiques des derniers jours \
et des prochains jours qui peuvent faire bouger l'actif : conflits et tensions \
militaires, sanctions, élections, guerres commerciales et droits de douane, décisions \
politiques sur les cryptomonnaies, crises bancaires ou souveraines, sentiment de \
« fuite vers la sécurité ».

Utilise la recherche web pour trouver l'actualité récente (privilégie les 7 derniers \
jours). Pour chaque événement : date, impact probable sur l'actif (sens et intensité), \
et si le marché l'a déjà intégré dans le prix. Signale les événements programmés à \
venir qui représentent un risque de volatilité.
"""
        + SIGNAL_FORMAT
    ),
)

SENTIMENT = AgentSpec(
    key="sentiment",
    name="Analyste sentiment",
    icon="🧭",
    web_search=True,
    system=(
        "Tu es l'analyste sentiment et positionnement de l'équipe.\n"
        + COMMON_RULES
        + """
Ton travail : mesurer comment les autres acteurs sont positionnés et ce qu'ils \
ressentent, pour repérer les excès. Pour l'or : rapport COT de la CFTC (position nette \
des « managed money » et son évolution), ratios long/short des clients des brokers \
(positionnement retail), tonalité des médias, demande pour les options. Pour le \
bitcoin : indice Fear & Greed, taux de financement (funding) et open interest des \
contrats perpétuels, liquidations récentes, ratio long/short, prime Coinbase, tonalité \
des réseaux sociaux.

Un sentiment extrême est souvent un signal contrarian (foule massivement acheteuse = \
risque de purge, peur extrême = opportunité), alors qu'un sentiment modéré qui \
s'améliore accompagne plutôt la tendance. Dis clairement dans quel cas on se trouve.

Tu reçois les données de sentiment déjà disponibles ; complète avec la recherche web \
en citant la date de chaque chiffre.
"""
        + SIGNAL_FORMAT
    ),
)

SPECIALISTS: tuple[AgentSpec, ...] = (FUNDAMENTAL, TECHNICAL, GEOPOLITICAL, SENTIMENT)


@dataclass
class AgentReport:
    key: str
    name: str
    icon: str
    text: str = ""
    direction: Optional[str] = None
    confidence: Optional[int] = None
    sources: list[dict] = field(default_factory=list)
    error: Optional[str] = None


_DIRECTION_RE = re.compile(r"Direction\s*:\s*\**\s*(HAUSSIER|BAISSIER|NEUTRE)", re.I)
_CONFIDENCE_RE = re.compile(r"Confiance\s*:\s*\**\s*(\d{1,3})\s*%", re.I)


def parse_signal(text: str) -> tuple[Optional[str], Optional[int]]:
    """Extrait la direction et la confiance de la section « Signal » d'un rapport."""
    d = _DIRECTION_RE.findall(text)
    c = _CONFIDENCE_RE.findall(text)
    direction = d[-1].upper() if d else None
    confidence = min(int(c[-1]), 100) if c else None
    return direction, confidence


def _collect(content, texts: list[str], sources: list[dict]) -> None:
    for block in content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                if getattr(r, "type", None) == "web_search_result":
                    sources.append({"title": r.title, "url": r.url})


def run_specialist(
    client: anthropic.Anthropic, spec: AgentSpec, briefing: str, model: str = config.MODEL
) -> AgentReport:
    report = AgentReport(key=spec.key, name=spec.name, icon=spec.icon)
    messages: list[dict] = [{"role": "user", "content": briefing}]
    texts: list[str] = []
    try:
        for _ in range(MAX_CONTINUATIONS):
            with client.beta.messages.stream(
                model=model,
                max_tokens=32000,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"effort": config.SPECIALIST_EFFORT},
                system=spec.system,
                tools=[WEB_SEARCH_TOOL] if spec.web_search else anthropic.omit,
                messages=messages,
            ) as stream:
                response = stream.get_final_message()

            if response.stop_reason == "refusal":
                report.error = "La requête a été refusée par le modèle."
                return report
            _collect(response.content, texts, report.sources)
            # La recherche web tourne côté serveur ; si la boucle serveur atteint sa
            # limite, on renvoie la réponse telle quelle pour qu'elle reprenne.
            if response.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": response.content}]
    except anthropic.APIError as e:
        report.error = f"Erreur API : {e}"
        return report

    report.text = "".join(texts).strip()
    report.direction, report.confidence = parse_signal(report.text)
    # Dédoublonnage des sources en gardant l'ordre.
    seen: set[str] = set()
    report.sources = [s for s in report.sources if not (s["url"] in seen or seen.add(s["url"]))]
    return report


# --------------------------------------------------------------------------- décision


class AgentView(BaseModel):
    agent: Literal["fondamental", "technique", "geopolitique", "sentiment"]
    direction: Literal["HAUSSIER", "BAISSIER", "NEUTRE"]
    poids_dans_la_decision: int = Field(description="Importance accordée à cet avis, de 0 à 100")
    commentaire: str = Field(description="Ce que tu retiens de ce rapport, en une ou deux phrases")


class TradingDecision(BaseModel):
    decision: Literal["ACHAT", "VENTE", "ATTENDRE"]
    confiance: int = Field(description="Confiance dans la décision, de 0 à 100")
    horizon: str
    synthese: str = Field(description="Explication de la décision en 4 à 8 phrases")
    prix_entree: Optional[float] = Field(description="Prix d'entrée visé ; null si ATTENDRE")
    zone_entree: str = Field(description="Zone ou condition d'entrée (ordre limite, cassure...)")
    stop_loss: Optional[float] = Field(description="Niveau de stop-loss ; null si ATTENDRE")
    take_profits: list[float] = Field(description="Objectifs de prix, du plus proche au plus lointain")
    vues_agents: list[AgentView]
    arguments_pour: list[str]
    arguments_contre: list[str]
    conditions_invalidation: list[str] = Field(
        description="Ce qui rendrait la décision caduque (niveau cassé, événement...)"
    )
    evenements_a_surveiller: list[str]


DECISION_SYSTEM = """Tu es le chef de desk d'une équipe de trading. Quatre analystes \
(fondamental, technique, géopolitique, sentiment) viennent de te remettre leurs rapports sur un \
actif. Ta mission : prendre UNE décision exploitable — ACHAT, VENTE ou ATTENDRE.

Principes :
- Pèse les rapports selon leur pertinence pour l'horizon de trading demandé : la \
technique compte davantage à court terme, le fondamental à plus long terme, la \
géopolitique surtout quand un événement majeur est en cours ou imminent, le \
sentiment surtout quand il est extrême (risque de retournement) ou qu'il contredit \
le mouvement des prix.
- Un rapport vague, ancien ou incertain pèse moins qu'un rapport précis et daté.
- Quand les analystes se contredisent sans argument décisif, ou que le rapport \
risque/rendement est inférieur à 1,5, ATTENDRE est une décision légitime.
- Pour ACHAT ou VENTE : l'entrée, le stop-loss et les objectifs doivent être cohérents \
avec les niveaux techniques et la volatilité (ATR) fournis. Pour un achat, \
stop < entrée < objectifs ; pour une vente, l'inverse.
- Sois honnête sur l'incertitude : la confiance doit refléter le degré d'accord entre \
les analystes et la qualité des informations.

Réponds en français."""


def build_decision_prompt(
    asset_name: str, price: float, style: str, reports: list[AgentReport], notes: str = ""
) -> str:
    parts = [
        f"Actif : {asset_name}",
        f"Prix actuel : {price:.2f}",
        f"Style / horizon de trading : {style}",
    ]
    if notes.strip():
        parts.append(f"Remarques du trader : {notes.strip()}")
    for r in reports:
        body = r.text if not r.error else f"(rapport indisponible : {r.error})"
        parts.append(f'\n<rapport agent="{r.key}" auteur="{r.name}">\n{body}\n</rapport>')
    parts.append("\nPrends ta décision.")
    return "\n".join(parts)


def run_decision(
    client: anthropic.Anthropic, prompt: str, model: str = config.MODEL
) -> TradingDecision:
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": config.DECISION_EFFORT},
        output_format=TradingDecision,
        system=DECISION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("Le chef de desk a refusé la requête.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("La décision n'a pas pu être générée complètement, relance l'analyse.")
    return response.parsed_output
