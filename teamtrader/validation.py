"""Contrôles déterministes d'une décision, avant affichage et enregistrement.

Les règles de cohérence sont aussi écrites dans le prompt du chef de desk, mais un
modèle peut s'en écarter : ce module les impose. Une décision qui ne passe pas les
contrôles est renvoyée une fois au chef de desk pour correction, puis rejetée
(transformée en ATTENDRE sans niveaux) si elle reste invalide.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional

MIN_RISK_REWARD = float(os.environ.get("TEAMTRADER_MIN_RR", "1.5"))
# Une entrée à plus de 10 % du prix actuel est presque sûrement une erreur.
MAX_ENTRY_DISTANCE_PCT = float(os.environ.get("TEAMTRADER_MAX_ENTRY_DISTANCE_PCT", "10"))


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def is_price(x: Optional[float]) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def direction_of(decision: str, entry: Optional[float], stop: Optional[float]) -> Optional[str]:
    """ACHAT / VENTE, ou déduit des niveaux (stop sous l'entrée = achat) pour un plan conditionnel."""
    if decision in ("ACHAT", "VENTE"):
        return decision
    if is_price(entry) and is_price(stop) and entry != stop:
        return "ACHAT" if stop < entry else "VENTE"
    return None


def risk_reward(entry: Optional[float], stop: Optional[float], target: Optional[float],
                direction: Optional[str] = None) -> Optional[float]:
    """Ratio gain / risque SIGNÉ : négatif si l'objectif est du mauvais côté de l'entrée.

    Renvoie None si le stop est du mauvais côté (risque nul ou négatif).
    """
    if not (is_price(entry) and is_price(stop) and is_price(target)):
        return None
    direction = direction or direction_of("", entry, stop)
    if direction == "ACHAT":
        risk, reward = entry - stop, target - entry
    elif direction == "VENTE":
        risk, reward = stop - entry, entry - target
    else:
        return None
    return reward / risk if risk > 0 else None


def validate_decision(d, price: float, min_rr: float = MIN_RISK_REWARD) -> ValidationResult:
    res = ValidationResult()
    err = res.errors.append

    if not 0 <= d.confiance <= 100:
        err(f"La confiance doit être comprise entre 0 et 100 (reçu : {d.confiance}).")
    for v in d.vues_agents:
        if not 0 <= v.poids_dans_la_decision <= 100:
            err(f"Le poids de l'agent {v.agent} doit être compris entre 0 et 100 "
                f"(reçu : {v.poids_dans_la_decision}).")

    levels = [("prix d'entrée", d.prix_entree), ("stop-loss", d.stop_loss)]
    levels += [(f"objectif {i}", t) for i, t in enumerate(d.take_profits, 1)]
    for name, value in levels:
        if value is not None and not is_price(value):
            err(f"Le {name} doit être un prix positif et fini (reçu : {value}).")

    if d.decision == "ATTENDRE":
        return res

    # --- prise de position : niveaux complets et cohérents
    if d.prix_entree is None:
        err("Un prix d'entrée est obligatoire pour un ACHAT ou une VENTE.")
    if d.stop_loss is None:
        err("Un stop-loss est obligatoire pour un ACHAT ou une VENTE.")
    if not d.take_profits:
        err("Au moins un objectif est obligatoire pour un ACHAT ou une VENTE.")
    if not res.ok:
        return res

    entry, stop, targets = d.prix_entree, d.stop_loss, d.take_profits
    long = d.decision == "ACHAT"
    if long and not stop < entry:
        err(f"Pour un achat, le stop ({stop}) doit être sous l'entrée ({entry}).")
    if not long and not stop > entry:
        err(f"Pour une vente, le stop ({stop}) doit être au-dessus de l'entrée ({entry}).")
    for i, t in enumerate(targets, 1):
        if long and not t > entry:
            err(f"Pour un achat, l'objectif {i} ({t}) doit être au-dessus de l'entrée ({entry}).")
        if not long and not t < entry:
            err(f"Pour une vente, l'objectif {i} ({t}) doit être sous l'entrée ({entry}).")
    ordered = all((b > a) if long else (b < a) for a, b in zip(targets, targets[1:]))
    if not ordered:
        err("Les objectifs doivent être ordonnés du plus proche au plus lointain.")

    if is_price(price) and abs(entry / price - 1) * 100 > MAX_ENTRY_DISTANCE_PCT:
        err(f"L'entrée ({entry}) est à plus de {MAX_ENTRY_DISTANCE_PCT:.0f} % du prix actuel "
            f"({price:.2f}).")

    rr = risk_reward(entry, stop, targets[0], d.decision)
    if res.ok and (rr is None or rr < min_rr):
        shown = "non calculable" if rr is None else f"{rr:.2f}"
        err(f"Le ratio risque / rendement sur l'objectif 1 ({shown}) est inférieur au minimum "
            f"de {min_rr:.1f}.")
    return res


def rejected(d, errors: list[str]):
    """Décision invalide après correction : ATTENDRE sans niveaux, avec les raisons."""
    reasons = " ; ".join(errors)
    return d.model_copy(update={
        "decision": "ATTENDRE",
        "confiance": max(0, min(100, d.confiance)),
        "prix_entree": None,
        "stop_loss": None,
        "take_profits": [],
        "zone_entree": "Aucune : décision rejetée par les contrôles automatiques.",
        "synthese": f"❌ Décision {d.decision} rejetée par les contrôles automatiques : "
                    f"{reasons}\n\n{d.synthese}",
    })
