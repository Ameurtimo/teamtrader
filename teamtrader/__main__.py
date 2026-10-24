"""Version ligne de commande : python -m teamtrader XAUUSD [--style swing] [--smc]"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import anthropic

from . import costs, history, logs, paper, pipeline, quality, risk
from .config import ASSETS, STYLE_MAX_HOURS, TRADING_STYLES

STYLE_LABELS = dict(zip(("intraday", "swing", "position"), TRADING_STYLES))
log = logging.getLogger("teamtrader.cli")


def main() -> None:
    p = argparse.ArgumentParser(description="TeamTrader — décision de trading multi-agents")
    p.add_argument("asset", choices=list(ASSETS))
    p.add_argument("--style", choices=list(STYLE_LABELS), default="swing")
    p.add_argument("--notes", default="")
    p.add_argument("--smc", action="store_true",
                   help="Mode Smart Money Concepts : entrée seulement après balayage de l'inducement")
    p.add_argument("--paper", action="store_true",
                   help="Ouvre un ordre fictif (paper trading) si la décision le permet")
    p.add_argument("--verbose", "-v", action="store_true", help="Affiche les rapports complets")
    args = p.parse_args()
    logs.setup()

    con = history.connect()
    allowed, spent = costs.budget_status(con)
    if not allowed:
        sys.exit(f"Budget IA du mois atteint ({spent:.2f} $) : analyse bloquée.")

    client = anthropic.Anthropic()
    started = time.monotonic()
    print(f"Récupération des données pour {ASSETS[args.asset].name}…")
    style = STYLE_LABELS[args.style]
    pcon = paper.connect()
    ctx = pipeline.prepare(args.asset, style, args.notes, smc_mode=args.smc,
                           portfolio=paper.portfolio_text(paper.load(pcon)))
    print(f"Prix actuel : {ctx.price:.2f} ({ctx.source})")
    for item in ctx.data_items:
        if item.status != quality.OK:
            print(f"  ⚠️ {item.name} : {item.status}")
    if ctx.smc_map:
        print(f"🎯 Setup SMC : {ctx.smc_map.setup.status}")
    print()

    reports = []
    for r in pipeline.run_specialists(client, ctx):
        reports.append(r)
        status = r.error or f"{r.direction or '?'} ({r.confidence or '?'}%)"
        print(f"{r.icon} {r.name} : {status} [{r.duration_s:.0f} s]")
        if args.verbose and r.text:
            print(f"\n{r.text}\n")

    print("\n🧠 Le chef de desk délibère…\n")
    result = pipeline.decide(client, ctx, reports)
    d = result.decision
    analysis = pipeline.analysis_record(ctx, reports, result, round(time.monotonic() - started, 1))
    costs.record(con, "analyse", analysis["consommation"])
    record_id = history.save(con, asset=args.asset, style=style, price=ctx.price, decision=d,
                             reports=reports, source=ctx.source,
                             mode="smc" if args.smc else "standard", provider=ctx.provider,
                             basis=ctx.basis, analysis=analysis)

    print(f"=== {d.decision} — confiance {d.confiance}% — {d.horizon} — contrôles : "
          f"{result.status} ===\n")
    for e in result.validation_errors:
        print(f"  🔧 corrigé : {e}")
    for b in result.blocked_by:
        print(f"  ⛔ {b}")
    print(d.synthese, "\n")
    if d.decision != "ATTENDRE":
        print(f"Entrée : {d.prix_entree} ({d.zone_entree})")
        print(f"Stop-loss : {d.stop_loss}")
        print(f"Objectifs : {', '.join(str(t) for t in d.take_profits)}")
        settings = risk.load_settings(pcon)
        positions = paper.load(pcon)
        acc = paper.account(positions, settings)
        s = risk.size_position(
            asset=args.asset, long=d.decision == "ACHAT", entry=d.prix_entree, stop=d.stop_loss,
            target=d.take_profits[0] if d.take_profits else None, settings=settings,
            equity=acc.equity, open_positions=[x.as_open_risk() for x in positions if x.is_open],
            realized_today=acc.today)
        if s.blocked:
            print("⛔ " + " ; ".join(s.reasons))
        else:
            print(f"Taille : {s.lots:g} lot(s), perte au stop {s.risk_at_stop:,.0f} $ coûts "
                  f"inclus, R/R net {s.net_rr:.2f}")
            if args.paper:
                paper.place_order(pcon, asset=args.asset, long=d.decision == "ACHAT", sizing=s,
                                  entry=d.prix_entree, stop=d.stop_loss,
                                  target=d.take_profits[0] if d.take_profits else None,
                                  price=ctx.price, max_hours=STYLE_MAX_HOURS[style],
                                  profile=settings.profiles[args.asset], provider=ctx.provider,
                                  basis=ctx.basis, decision_id=record_id)
                print("📝 Ordre fictif enregistré (paper trading).")
    print("\nInvalidation :", *(f"  - {x}" for x in d.conditions_invalidation), sep="\n")
    tot = analysis["couts"]
    print(f"\n💰 Coût estimé : {tot['cost_usd']:.2f} $ ({tot['web_searches']} recherche(s) web)")
    print("⚠️  Aide à la décision uniquement, pas un conseil en investissement.")


if __name__ == "__main__":
    main()
