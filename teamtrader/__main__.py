"""Version ligne de commande : python -m teamtrader XAUUSD [--style swing]"""

from __future__ import annotations

import argparse

import anthropic

from . import history, pipeline
from .config import ASSETS, TRADING_STYLES

STYLE_LABELS = dict(zip(("intraday", "swing", "position"), TRADING_STYLES))


def main() -> None:
    p = argparse.ArgumentParser(description="TeamTrader — décision de trading multi-agents")
    p.add_argument("asset", choices=list(ASSETS))
    p.add_argument("--style", choices=list(STYLE_LABELS), default="swing")
    p.add_argument("--notes", default="")
    p.add_argument("--verbose", "-v", action="store_true", help="Affiche les rapports complets")
    args = p.parse_args()

    client = anthropic.Anthropic()
    print(f"Récupération des données pour {ASSETS[args.asset].name}…")
    style = STYLE_LABELS[args.style]
    ctx = pipeline.prepare(args.asset, TRADING_STYLES[style], args.notes)
    print(f"Prix actuel : {ctx.price:.2f} ({ctx.source})\n")

    reports = []
    for r in pipeline.run_specialists(client, ctx):
        reports.append(r)
        status = r.error or f"{r.direction or '?'} ({r.confidence or '?'}%)"
        print(f"{r.icon} {r.name} : {status}")
        if args.verbose and r.text:
            print(f"\n{r.text}\n")

    print("\n🧠 Le chef de desk délibère…\n")
    d = pipeline.decide(client, ctx, reports)
    with history.connect() as con:
        history.save(con, asset=args.asset, style=style, price=ctx.price, decision=d,
                     reports=reports, source=ctx.source)
    print(f"=== {d.decision} — confiance {d.confiance}% — {d.horizon} ===\n")
    print(d.synthese, "\n")
    if d.decision != "ATTENDRE":
        print(f"Entrée : {d.prix_entree} ({d.zone_entree})")
        print(f"Stop-loss : {d.stop_loss}")
        print(f"Objectifs : {', '.join(str(t) for t in d.take_profits)}")
    print("\nInvalidation :", *(f"  - {x}" for x in d.conditions_invalidation), sep="\n")
    print("\n⚠️  Aide à la décision uniquement, pas un conseil en investissement.")


if __name__ == "__main__":
    main()
