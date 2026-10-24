"""Interface web TeamTrader — lancer avec : streamlit run app.py"""

from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="TeamTrader", page_icon="📈", layout="wide")


def _load_secrets() -> None:
    """Sur Streamlit Community Cloud, les clés sont dans st.secrets : on les expose
    en variables d'environnement avant d'importer la configuration."""
    try:
        for k, v in st.secrets.items():
            if isinstance(v, str) and not os.environ.get(k):
                os.environ[k] = v
    except Exception:
        pass  # pas de fichier secrets.toml en local : rien à faire


_load_secrets()

from teamtrader import (  # noqa: E402
    agents, backup, calendar_history, config, costs, data, economic_calendar, history,
    indicators, logs, paper, pipeline, quality, risk, smc, validation,
)
from teamtrader.config import ASSETS, TRADING_STYLES  # noqa: E402

logs.setup()
log = logging.getLogger("teamtrader.app")

DIRECTION_BADGE = {"HAUSSIER": "🟢 HAUSSIER", "BAISSIER": "🔴 BAISSIER", "NEUTRE": "⚪ NEUTRE"}
DECISION_COLOR = {"ACHAT": "#16a34a", "VENTE": "#dc2626", "ATTENDRE": "#6b7280"}


def price_chart(ctx: pipeline.Context):
    df = ctx.frames["4h"].tail(150)
    close = ctx.frames["4h"]["Close"]
    fig = go.Figure(
        go.Candlestick(
            x=df.index, open=df.Open, high=df.High, low=df.Low, close=df.Close, name="4h"
        )
    )
    for n, color in ((20, "#f59e0b"), (50, "#3b82f6")):
        fig.add_scatter(
            x=df.index, y=indicators.ema(close, n).loc[df.index], name=f"EMA{n}",
            line={"width": 1.2, "color": color},
        )
    fig.update_layout(
        height=420, margin={"l": 10, "r": 10, "t": 30, "b": 10},
        xaxis_rangeslider_visible=False, title=f"{ctx.asset.name} — bougies 4h",
    )
    return fig


def add_levels(fig, d: agents.TradingDecision):
    if d.prix_entree:
        fig.add_hline(y=d.prix_entree, line_dash="dot", line_color="#2563eb",
                      annotation_text="Entrée")
    if d.stop_loss:
        fig.add_hline(y=d.stop_loss, line_dash="dot", line_color="#dc2626",
                      annotation_text="Stop")
    for i, tp in enumerate(d.take_profits, 1):
        fig.add_hline(y=tp, line_dash="dot", line_color="#16a34a", annotation_text=f"TP{i}")


# ------------------------------------------------------------------------------ SMC

SETUP_COLOR = {True: "#16a34a", False: "#d97706"}


def smc_chart(df: pd.DataFrame, m: smc.SMCMap, title: str):
    df = df.tail(220)
    start = df.index[0]
    fig = go.Figure(go.Candlestick(x=df.index, open=df.Open, high=df.High, low=df.Low,
                                   close=df.Close, name=m.timeframe))
    end = df.index[-1]
    zones = [z for z in m.order_blocks if z.state != "invalidé"][-4:] + m.fvgs[-4:]
    if m.setup.poi is not None and m.setup.poi not in zones:
        zones.append(m.setup.poi)
    for z in zones:
        if z.ts < start:
            continue
        bull = z.direction == "haussier"
        color = ("rgba(22,163,74,{a})" if bull else "rgba(220,38,38,{a})")
        alpha = 0.22 if z.kind == "OB" else 0.10
        fig.add_shape(type="rect", x0=z.ts, x1=end, y0=z.bottom, y1=z.top, line_width=0,
                      fillcolor=color.format(a=alpha), layer="below")
        fig.add_annotation(x=z.ts, y=z.top, text=z.kind, showarrow=False, xanchor="left",
                           yanchor="bottom", font={"size": 9})
    for b in m.breaks[-6:]:
        if b.ts < start:
            continue
        fig.add_shape(type="line", x0=max(b.swing_ts, start), x1=b.ts, y0=b.level, y1=b.level,
                      line={"width": 1, "dash": "dot",
                            "color": "#16a34a" if b.direction == "haussier" else "#dc2626"})
        fig.add_annotation(x=b.ts, y=b.level, text=b.kind, showarrow=False, yanchor="bottom",
                           font={"size": 9})
    for liq in m.liquidity:
        fig.add_hline(y=liq.price, line_dash="dash", line_width=1, line_color="#7c3aed",
                      annotation_text=f"liquidité ({liq.kind})", annotation_font_size=9)
    s = m.setup
    if s.idm_level is not None:
        fig.add_hline(y=s.idm_level, line_width=1.5, line_color="#d97706",
                      annotation_text="IDM ✓ balayé" if s.idm_swept else "IDM (non balayé)",
                      annotation_font_size=10)
    if s.equilibrium is not None:
        fig.add_hline(y=s.equilibrium, line_dash="dot", line_width=1, line_color="#6b7280",
                      annotation_text="50 %", annotation_font_size=9)
    fig.update_layout(height=480, margin={"l": 10, "r": 10, "t": 30, "b": 10},
                      xaxis_rangeslider_visible=False, title=title, showlegend=False)
    return fig


def render_smc(m: smc.SMCMap, df: pd.DataFrame, asset_name: str):
    s = m.setup
    color = "#dc2626" if s.status.startswith("invalidé") else SETUP_COLOR[s.entry_allowed]
    verdict = "ENTRÉE VALIDE" if s.entry_allowed else "PAS D'ENTRÉE"
    st.markdown(
        f"<div style='padding:14px;border-radius:12px;background:{color};color:white'>"
        f"<div style='font-size:13px;opacity:.85'>Smart Money Concepts · {m.timeframe}"
        f" (biais {m.htf_timeframe} : {m.htf_trend or 'indéterminé'})</div>"
        f"<div style='font-size:26px;font-weight:700'>{verdict} · setup "
        f"{s.direction or '—'}</div><div style='font-size:15px'>{s.status}</div></div>",
        unsafe_allow_html=True,
    )
    st.write("")
    c = st.columns(5)
    idm_delta = None if s.idm_level is None else ("balayé ✓" if s.idm_swept else "non balayé")
    c[0].metric("Inducement", fmt(s.idm_level), idm_delta,
                delta_color="normal" if s.idm_swept else "off")
    c[1].metric("POI", f"{s.poi.bottom:,.2f}–{s.poi.top:,.2f}" if s.poi else "—",
                s.poi.kind if s.poi else None, delta_color="off", delta_arrow="off")
    c[2].metric("Entrée", fmt(s.entry))
    c[3].metric("Stop", fmt(s.stop))
    c[4].metric("Objectifs", " / ".join(f"{t:,.0f}" for t in s.targets) or "—")
    for n in s.notes:
        st.markdown(f"- {n}")
    st.plotly_chart(smc_chart(df, m, f"{asset_name} — SMC {m.timeframe}"), width="stretch")
    with st.expander("Cartographie complète"):
        st.code(smc.format_map(m), language=None)


@st.cache_data(ttl=300, show_spinner="Scan SMC…")
def smc_scan(asset_key: str, style_label: str):
    # Les erreurs sont mises en cache aussi : une source en panne ne ralentit pas chaque clic.
    try:
        prices = data.get_price_history(ASSETS[asset_key], with_15m=True)
        tf, _ = smc.STYLE_TIMEFRAMES[style_label]
        return (pipeline.smc_for(prices.frames, style_label), prices.frames[tf],
                prices.source), None
    except Exception as e:
        return None, str(e)


def render_smc_scanner(asset_key: str, style_label: str):
    st.caption("Lecture SMC calculée automatiquement à partir des prix — instantanée et "
               "gratuite (aucun appel à l'IA). Règle appliquée : une entrée n'est valide "
               "qu'après le balayage de l'inducement.")
    if st.button("🔄 Rescanner"):
        smc_scan.clear()
    result, error = smc_scan(asset_key, style_label)
    if error:
        st.error(f"Impossible de récupérer les prix : {error}")
        return
    m, df, source = result
    render_smc(m, df, ASSETS[asset_key].name)
    st.caption(f"Source des prix : {source}")


# ------------------------------------------------------------------------------ calendrier

ARROW = {"AU-DESSUS": "⬆️ au-dessus", "EN DESSOUS": "⬇️ en dessous", "CONFORME": "➡️ conforme"}


def utc_to_local(value: str) -> str:
    try:
        t = datetime.strptime(value.strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    return economic_calendar.local_time(t, config.DISPLAY_TZ)


@st.cache_data(ttl=1800, show_spinner="Chargement du calendrier…")
def calendar_events():
    try:
        return economic_calendar.fetch_us_events(), None
    except Exception as e:
        return [], str(e)


def calendar_summary(forecast: agents.CalendarForecast) -> str:
    return "\n".join(
        f"- {e.date_heure_utc} UTC — {e.evenement} : {e.prevision} du consensus "
        f"({e.consensus}), estimation {e.estimation}, probabilité {e.probabilite} % ; "
        f"réaction attendue or {e.reaction_or}, BTC {e.reaction_btc}, USD {e.reaction_usd}"
        for e in forecast.evenements
    ) + f"\nSynthèse : {forecast.synthese}"


def render_calendar(api_key: str):
    tz = config.DISPLAY_TZ
    events, error = calendar_events()
    if error:
        st.warning(f"Calendrier indisponible ({error}) : l'économiste IA le retrouvera par "
                   "recherche web.")
        calendar_text = "Calendrier indisponible : retrouve-le par recherche web."
    else:
        st.markdown(f"**Publications américaines de la semaine** (impact fort et moyen, "
                    f"heures {tz})")
        st.markdown(economic_calendar.format_events(events, tz))
        calendar_text = economic_calendar.format_events(events)

    st.write("")
    if st.button("🔮 Prédire la direction des publications", type="primary"):
        if not api_key:
            st.error("Renseigne ta clé API Anthropic.")
        else:
            with st.spinner("L'économiste analyse les indicateurs avancés (1 à 3 minutes)…"):
                try:
                    forecast, report = agents.forecast_calendar(
                        anthropic.Anthropic(api_key=api_key), calendar_text,
                        track_record=predictor_track_record())
                except Exception as e:
                    forecast, report = None, agents.AgentReport(
                        key="calendrier", name="", icon="", error=str(e))
            try:
                costs.record(history.connect(), "calendrier", report.usage)
            except Exception:
                log.exception("Coûts du calendrier non enregistrés")
            if forecast is not None:
                try:
                    with calendar_history.connect() as con:
                        calendar_history.save_forecast(con, forecast)
                except Exception as e:
                    st.warning(f"Prévisions non enregistrées pour le suivi : {e}")
            st.session_state["calendar"] = (datetime.now(), forecast, report)

    if "calendar" not in st.session_state:
        st.info("Clique sur le bouton pour obtenir une prévision (au-dessus / en dessous du "
                "consensus) de chaque publication importante, avant sa sortie. Les "
                "prévisions sont ensuite transmises au chef de desk lors des analyses.")
        return
    when, forecast, report = st.session_state["calendar"]
    if forecast is None:
        st.error(report.error or "Prévision indisponible.")
        return
    st.caption(f"Prévisions générées le {when:%d/%m à %H:%M}")
    st.markdown(f"**Synthèse :** {forecast.synthese}")
    st.dataframe(
        [
            {
                f"Date ({config.DISPLAY_TZ})": utc_to_local(e.date_heure_utc),
                "Publication": e.evenement,
                "Impact": e.importance, "Précédent": e.precedent, "Consensus": e.consensus,
                "Prévision IA": ARROW[e.prevision], "Estimation": e.estimation,
                "Probabilité": f"{e.probabilite}%", "Or": DIRECTION_BADGE[e.reaction_or],
                "BTC": DIRECTION_BADGE[e.reaction_btc], "Dollar": DIRECTION_BADGE[e.reaction_usd],
            }
            for e in forecast.evenements
        ],
        hide_index=True, width="stretch",
    )
    for e in forecast.evenements:
        with st.expander(f"{ARROW[e.prevision]} · {e.evenement} ({utc_to_local(e.date_heure_utc)})"):
            st.markdown(e.explication)
            st.markdown("**Indicateurs avancés :**\n" + "\n".join(f"- {x}" for x in e.indicateurs_avances))
    st.info(f"**Gestion du risque :** {forecast.gestion_du_risque}")
    with st.expander("Note complète de l'économiste et sources"):
        st.markdown(report.text)
        for src in report.sources[:15]:
            st.markdown(f"- [{src['title']}]({src['url']})")
    st.caption("Une prévision n'est pas une certitude : même les meilleurs économistes se "
               "trompent souvent sur l'écart au consensus. Le mouvement réel dépend aussi "
               "des révisions et du détail des chiffres.")


# ------------------------------------------------------------------------------ fiabilité du prédicteur

CHECK = {True: "✅", False: "❌", None: "—"}


def predictor_track_record() -> str:
    try:
        con = calendar_history.connect()
        evals = calendar_history.evaluate(calendar_history.load(con), {})
    except Exception:
        return ""
    return calendar_history.track_record_text(calendar_history.statistics(evals))


def reaction_candles(rows: list[calendar_history.ForecastRow]) -> dict[str, pd.DataFrame]:
    times = [r.event_time for r in rows if r.released]
    if not times:
        return {}
    start = (min(times) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    out = {}
    for key in ASSETS:
        try:
            out[key] = candles_since(key, start.isoformat())
        except Exception:
            pass  # la réaction de cet actif ne sera simplement pas évaluée
    return out


def render_forecast_tracking(api_key: str):
    st.divider()
    st.subheader("📊 Fiabilité du prédicteur")
    con = calendar_history.connect()
    rows = calendar_history.load(con)
    if not rows:
        st.info("Les prévisions sont enregistrées automatiquement. Une fois les chiffres "
                "publiés, leur fiabilité apparaîtra ici.")
        return

    # 1. Chiffres publiés disponibles dans le flux du calendrier : remplis automatiquement.
    events, _ = calendar_events()
    for row_id, value in calendar_history.match_feed_actuals(rows, events).items():
        calendar_history.set_actual(con, row_id, value, "calendrier")
    rows = calendar_history.load(con)

    # 2. Sinon, recherche IA (ou saisie manuelle dans le tableau).
    pending = calendar_history.pending_actuals(rows)
    if pending:
        c = st.columns([2, 3])
        if c[0].button(f"🔎 Récupérer les {len(pending)} chiffres publiés manquants (IA)"):
            if not api_key:
                st.error("Renseigne ta clé API Anthropic.")
            else:
                items = [{"id": r.id, "evenement": r.event,
                          "date_heure_utc": f"{r.event_time:%Y-%m-%d %H:%M}",
                          "consensus": r.consensus} for r in pending]
                with st.spinner("Recherche des chiffres publiés…"):
                    try:
                        found, vreport = agents.fetch_actuals(
                            anthropic.Anthropic(api_key=api_key), items)
                        costs.record(con, "verificateur", vreport.usage)
                    except Exception as e:
                        found = {}
                        st.error(f"Recherche impossible : {e}")
                saved = 0
                for row_id, value in found.items():
                    if value and value.lower() != "introuvable":
                        calendar_history.set_actual(con, row_id, value, "IA (recherche web)")
                        saved += 1
                st.success(f"{saved} chiffre(s) retrouvé(s).")
                rows = calendar_history.load(con)
        c[1].caption("Tu peux aussi saisir le chiffre publié directement dans le tableau "
                     "ci-dessous (colonne « Chiffre publié »).")

    evals = calendar_history.evaluate(rows, reaction_candles(rows))
    stats = calendar_history.statistics(evals)

    def rate(r):
        return f"{r['taux']:.0f}%" if r else "—"

    def label(name, r):
        return f"{name} ({r['justes']}/{r['n']})" if r else name

    m = st.columns(4)
    m[0].metric(label("Prévisions justes", stats["prevision"]), rate(stats["prevision"]),
                help="Au-dessus / en dessous / conforme au consensus. "
                "Un tirage au hasard ferait environ 33 %.")
    m[1].metric(label("Hors « conforme »", stats["prevision_hors_conforme"]),
                rate(stats["prevision_hors_conforme"]),
                help="Seulement quand l'économiste annonçait une surprise.")
    m[2].metric(label("Réaction de l'or (1 h)", stats["reaction_or"]), rate(stats["reaction_or"]))
    m[3].metric(label("Réaction du BTC (1 h)", stats["reaction_btc"]), rate(stats["reaction_btc"]))

    calib = [{"Probabilité annoncée": k, "Prévisions": v["n"], "Taux de réussite": rate(v)}
             for k, v in stats["par_probabilite"].items() if v]
    if calib:
        st.caption("Calibration : quand l'économiste annonce 65 %, a-t-il raison 65 % du temps ?")
        st.dataframe(calib, hide_index=True)

    table = pd.DataFrame([
        {
            "id": e.row.id,
            "Date": economic_calendar.local_time(e.row.event_time, config.DISPLAY_TZ)
                    if e.row.event_time else "—",
            "Publication": e.row.event,
            "Consensus": e.row.consensus,
            "Prévision": ARROW.get(e.row.prediction, e.row.prediction),
            "Proba": f"{e.row.probability}%" if e.row.probability is not None else "—",
            "Chiffre publié": e.row.actual or "",
            "Résultat": ARROW.get(e.outcome, "—") if e.outcome else "—",
            "Juste ?": CHECK[e.correct],
            "Or 1 h": fmt(e.gold_move, "{:+.2f}%") + " " + CHECK[e.gold_ok],
            "BTC 1 h": fmt(e.btc_move, "{:+.2f}%") + " " + CHECK[e.btc_ok],
        }
        for e in evals
    ])
    edited = st.data_editor(
        table, hide_index=True, width="stretch", key="forecast_editor",
        column_config={"id": None},
        disabled=[c for c in table.columns if c != "Chiffre publié"],
    )
    changes = [(int(row["id"]), str(row["Chiffre publié"] or ""))
               for (_, row), (_, before) in zip(edited.iterrows(), table.iterrows())
               if (row["Chiffre publié"] or "") != (before["Chiffre publié"] or "")]
    if changes:
        for row_id, value in changes:
            calendar_history.set_actual(con, row_id, value, "manuel")
        st.rerun()
    st.caption("Si une publication a été prévue plusieurs fois, seule la dernière prévision "
               "faite avant sa sortie compte. La réaction est mesurée entre l'ouverture de la "
               "bougie 1 h de la publication et sa clôture (variation sous 0,10 % pour l'or et "
               "0,25 % pour le BTC = neutre).")


def render_report(r: agents.AgentReport):
    badge = DIRECTION_BADGE.get(r.direction or "", "❔ non précisé")
    conf = f" — confiance {r.confidence}%" if r.confidence is not None else ""
    with st.expander(f"{r.icon} {r.name} : {badge}{conf}", expanded=False):
        if r.error:
            st.error(r.error)
        else:
            st.markdown(r.text)
        if r.sources:
            st.caption("Sources consultées")
            for s in r.sources[:12]:
                st.markdown(f"- [{s['title']}]({s['url']})")


def render_quality(q: Optional[quality.QualityReport], items: list[quality.DataItem]):
    icon = {quality.OK: "🟢", quality.STALE: "🟠", quality.MISSING: "🔴"}
    score = q.score if q else None
    label = f"🧪 Qualité des données{f' : {score} %' if score is not None else ''}"
    with st.expander(label, expanded=bool(q and not q.ok)):
        st.dataframe(
            [{"Donnée": i.name, "Statut": f"{icon.get(i.status, '')} {i.status}",
              "À jour au (UTC)": (i.as_of or "—").replace("T", " ")[:16],
              "Essentielle": "oui" if i.essential else "", "Source": i.source,
              "Détail": i.detail} for i in items],
            hide_index=True, width="stretch",
        )
        if q:
            for name, status in q.report_status.items():
                if status != quality.OK:
                    st.caption(f"Rapport {name} : {status}")


def render_decision(ctx: pipeline.Context, result: pipeline.DecisionResult,
                    record_id: Optional[int]):
    d = result.decision
    color = DECISION_COLOR[d.decision]
    st.markdown(
        f"<div style='padding:18px;border-radius:12px;background:{color};color:white'>"
        f"<div style='font-size:14px;opacity:.85'>Décision du chef de desk · contrôles : "
        f"{result.status}</div>"
        f"<div style='font-size:34px;font-weight:700'>{d.decision} · {ctx.asset.name}</div>"
        f"<div style='font-size:16px'>Confiance {d.confiance}% · Horizon : {d.horizon}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    if result.retried and not result.rejected:
        st.info("🔧 La première proposition a été corrigée après les contrôles automatiques : "
                + " ; ".join(result.validation_errors))
    for reason in result.blocked_by:
        st.warning(reason)
    st.markdown(d.synthese)

    tp1 = d.take_profits[0] if d.take_profits else None
    direction = validation.direction_of(d.decision, d.prix_entree, d.stop_loss)
    rr = validation.risk_reward(d.prix_entree, d.stop_loss, tp1, direction)
    c = st.columns(5)
    c[0].metric("Prix actuel", f"{ctx.price:,.2f}")
    c[1].metric("Entrée", fmt(d.prix_entree))
    c[2].metric("Stop-loss", fmt(d.stop_loss))
    c[3].metric("Objectif 1", fmt(tp1))
    c[4].metric("Risque / rendement brut (TP1)", fmt(rr, "{:.2f}"))
    targets = " → ".join(f"{t:,.2f}" for t in d.take_profits) or "—"
    st.caption(f"Entrée : {d.zone_entree} · Objectifs : {targets}")

    if d.decision in ("ACHAT", "VENTE"):
        render_sizing(ctx, d, record_id)

    render_quality(result.quality, ctx.data_items)
    usages = [u for r in st.session_state["result"][1] for u in r.usage] + result.usage
    tot = costs.total(usages)
    st.caption(f"💰 Coût estimé de l'analyse : {tot['cost_usd']:.2f} $ "
               f"({tot['input_tokens'] + tot['output_tokens']:,} tokens, "
               f"{tot['web_searches']} recherche(s) web) · modèle : {result.model or '—'}")

    if d.vues_agents:
        st.subheader("Avis des analystes")
        st.dataframe(
            [{"Agent": v.agent, "Direction": DIRECTION_BADGE[v.direction],
              "Poids": f"{v.poids_dans_la_decision}%", "Ce que le chef en retient": v.commentaire}
             for v in d.vues_agents],
            hide_index=True, width="stretch",
        )
    a, b = st.columns(2)
    with a:
        st.markdown("**✅ Arguments pour**")
        st.markdown("\n".join(f"- {x}" for x in d.arguments_pour) or "—")
        st.markdown("**🛑 Invalidation**")
        st.markdown("\n".join(f"- {x}" for x in d.conditions_invalidation) or "—")
    with b:
        st.markdown("**⚠️ Arguments contre**")
        st.markdown("\n".join(f"- {x}" for x in d.arguments_contre) or "—")
        st.markdown("**📅 À surveiller**")
        st.markdown("\n".join(f"- {x}" for x in d.evenements_a_surveiller) or "—")


def render_sizing(ctx: pipeline.Context, d: agents.TradingDecision, record_id: Optional[int]):
    con = paper.connect()
    settings = risk.load_settings(con)
    positions = paper.load(con)
    acc = paper.account(positions, settings)
    long = d.decision == "ACHAT"
    s = risk.size_position(
        asset=ctx.asset.key, long=long, entry=d.prix_entree, stop=d.stop_loss,
        target=d.take_profits[0] if d.take_profits else None, settings=settings,
        equity=acc.equity, open_positions=[p.as_open_risk() for p in positions if p.is_open],
        realized_today=acc.today,
    )
    st.markdown("**📐 Taille de position et coûts** (profil broker et limites : onglet "
                "Paper trading)")
    if s.blocked:
        for reason in s.reasons:
            st.error(f"⛔ {reason}")
        return
    c = st.columns(5)
    c[0].metric("Taille", f"{s.lots:g} lot(s)", f"{s.units:g} unités", delta_color="off", delta_arrow="off")
    c[1].metric("Nominal", f"{s.notional:,.0f} $", f"levier ×{s.leverage:.2f}", delta_color="off", delta_arrow="off")
    c[2].metric("Marge", f"{s.margin:,.0f} $")
    c[3].metric("Perte au stop (coûts inclus)", f"{s.risk_at_stop:,.0f} $",
                f"{s.risk_at_stop / acc.equity * 100:.2f} % des fonds propres", delta_color="off", delta_arrow="off")
    c[4].metric("R/R net de coûts", fmt(s.net_rr, "{:.2f}"), f"coûts ≈ {s.costs:,.2f} $",
                delta_color="off", delta_arrow="off")
    for w in s.warnings:
        st.caption(f"⚠️ {w}")
    if st.button("📝 Ouvrir en paper trading", key="paper_open"):
        paper.place_order(
            con, asset=ctx.asset.key, long=long, sizing=s, entry=d.prix_entree,
            stop=d.stop_loss, target=d.take_profits[0] if d.take_profits else None,
            price=ctx.price, max_hours=config.STYLE_MAX_HOURS.get(ctx.style_label, 240),
            profile=settings.profiles[ctx.asset.key], provider=ctx.provider, basis=ctx.basis,
            decision_id=record_id,
        )
        st.success("Ordre fictif enregistré : suis-le dans l'onglet 💼 Paper trading.")


# ------------------------------------------------------------------------------ historique

STATUS_ICON = {
    "objectif": "✅ objectif", "stop": "❌ stop", "expiré": "⏱️ expiré",
    "en cours": "🔄 en cours", "en attente d'entrée": "⏳ attente entrée",
    "non déclenché": "➖ non déclenché", "pas de trade": "⚪ pas de trade",
    "en attente de données": "… données",
}


AGENT_LABELS = {s.key: f"{s.icon} {s.name}" for s in agents.DECISION_AGENTS}
AGENT_LABELS["chef de desk"] = "🧠 Chef de desk (décision finale)"


@st.cache_data(ttl=900, show_spinner=False)
def candles_since(asset_key: str, start_iso: str) -> pd.DataFrame:
    start = datetime.fromisoformat(start_iso)
    return data.get_price_history(ASSETS[asset_key], start=start).frames["1h"]


@st.cache_data(ttl=600, show_spinner=False)
def _raw_candles(asset_key: str, provider: str, start_iso: str, tf: str):
    # Les erreurs sont mises en cache aussi : une source en panne ne ralentit pas chaque clic.
    try:
        return data.get_candles(ASSETS[asset_key], provider, datetime.fromisoformat(start_iso),
                                tf), None
    except Exception as e:
        return None, str(e)


def fetch_raw(asset_key: str, provider: str, start: datetime, tf: str) -> pd.DataFrame:
    start = start.replace(minute=0, second=0, microsecond=0)
    df, err = _raw_candles(asset_key, provider, start.isoformat(), tf)
    if err:
        raise data.MarketDataError(err)
    return df


@st.cache_data(ttl=1800, show_spinner=False)
def legacy_basis(asset_key: str) -> float:
    try:
        return data.current_basis(ASSETS[asset_key])
    except Exception:
        return 0.0


def fmt(v, pattern="{:,.2f}"):
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    if v == float("inf"):
        return "∞"
    return pattern.format(v)


def r_curve_chart(curve: list[tuple], title: str, y_title: str):
    fig = go.Figure(go.Scatter(x=[c[0] for c in curve], y=[c[1] for c in curve],
                               mode="lines+markers", line={"width": 2}))
    fig.update_layout(height=280, margin={"l": 10, "r": 10, "t": 30, "b": 10}, title=title,
                      yaxis_title=y_title)
    return fig


def breakdown_rows(groups: dict) -> list[dict]:
    return [{"Groupe": k, "Trades": v["trades_clotures"], "Objectifs": v["objectifs"],
             "Stops": v["stops"], "Expirés +/−": f"{v['expires_gagnants']}/{v['expires_perdants']}",
             "Gagnants": fmt(v["taux_gagnants"], "{:.0f}%"), "R moyen": fmt(v["r_moyen"], "{:+.2f}"),
             "R cumulé": fmt(v["r_total"], "{:+.2f}"), "Profit factor": fmt(v["profit_factor"], "{:.2f}")}
            for k, v in groups.items() if v["trades_clotures"]]


def render_analysis_detail(rec: history.Record, out: history.Outcome):
    st.markdown(f"**Décision N° {rec.id}** — {rec.created_at:%Y-%m-%d %H:%M} UTC — "
                f"{rec.asset} — {rec.decision} — résultat : {STATUS_ICON.get(out.status, out.status)}")
    st.caption(f"Source des prix : {rec.source or '—'} · fournisseur : {rec.provider or 'inconnu'} · "
               f"écart future/spot à la décision : {fmt(rec.basis)} · méthode : {out.method}"
               f"{' (résultat figé)' if out.frozen else ''}")
    if out.data_note:
        st.caption(f"Données : {out.data_note}")
    for a in out.ambiguities:
        st.warning(f"Ambiguïté : {a}")
    a = rec.analysis
    if not a:
        st.info("Analyse détaillée non conservée (décision antérieure à cette fonctionnalité).")
        return
    p, dec = a.get("parametres", {}), a.get("decision", {})
    st.markdown(f"Contrôles : **{dec.get('statut', '—')}** · modèle : {dec.get('modele') or '—'} · "
                f"coût : {a.get('couts', {}).get('cost_usd', 0):.2f} $ · durée : "
                f"{fmt(a.get('duree_s'), '{:.0f} s')}")
    for e in dec.get("erreurs_validation") or []:
        st.caption(f"Erreur corrigée : {e}")
    for b in dec.get("bloquee_par") or []:
        st.caption(f"Bloquée : {b}")
    with st.expander("Paramètres de l'analyse"):
        st.json(p)
    if a.get("qualite"):
        q = a["qualite"]
        with st.expander(f"Qualité des données : {q.get('score')} %"):
            st.dataframe(q.get("items", []), hide_index=True, width="stretch")
    if a.get("setup_smc"):
        with st.expander("Setup SMC au moment de la décision"):
            st.code(a["setup_smc"], language=None)
    for r in a.get("rapports", []):
        badge = DIRECTION_BADGE.get(r.get("direction") or "", "❔")
        with st.expander(f"{AGENT_LABELS.get(r['agent'], r['agent'])} : {badge} "
                         f"({r.get('modele') or '—'}, {fmt(r.get('duree_s'), '{:.0f} s')})"):
            if r.get("erreur"):
                st.error(r["erreur"])
            st.markdown(r.get("texte") or "")
            for s in (r.get("sources") or [])[:15]:
                st.markdown(f"- [{s['title']}]({s['url']})")


def render_history():
    con = history.connect()
    records = history.load(con)
    if not records:
        st.info("Aucune décision enregistrée pour l'instant : chaque analyse terminée "
                "est ajoutée automatiquement à l'historique.")
        render_maintenance()
        return
    top = st.columns([1, 2])
    if top[0].button("🔄 Actualiser les prix"):
        _raw_candles.clear()
    mode = top[1].radio("Décisions", ["Toutes", "Standard", "SMC"], horizontal=True,
                        label_visibility="collapsed")
    if mode != "Toutes":
        records = [r for r in records if r.mode == mode.lower()]
        if not records:
            st.info(f"Aucune décision en mode {mode}.")
            return
    pairs = history.evaluate_all(con, records, fetch_raw, legacy_basis)
    stats = history.statistics(pairs)

    st.subheader("📈 Performance")
    c = st.columns(6)
    c[0].metric("Décisions", stats["decisions"], f"{stats['en_cours']} en cours", delta_color="off", delta_arrow="off")
    c[1].metric("Trades clôturés", stats["trades_clotures"],
                f"{stats['objectifs']} obj. · {stats['stops']} stops", delta_color="off", delta_arrow="off")
    c[2].metric("Objectif avant stop", fmt(stats["taux_objectif"], "{:.0f}%"),
                help="Objectifs / (objectifs + stops), sans les clôtures à l'échéance")
    c[3].metric("Trades gagnants", fmt(stats["taux_gagnants"], "{:.0f}%"),
                f"expirés : {stats['expires_gagnants']} + / {stats['expires_perdants']} −",
                delta_color="off", delta_arrow="off", help="Part des trades clôturés (échéances comprises) avec R > 0")
    c[4].metric("R moyen · cumulé", fmt(stats["r_moyen"], "{:+.2f}"),
                fmt(stats["r_total"], "{:+.2f} R"), delta_color="off", delta_arrow="off",
                help="1R = la perte si le stop est touché")
    c[5].metric("Profit factor · drawdown", fmt(stats["profit_factor"], "{:.2f}"),
                fmt(stats["drawdown_max_r"], "{:+.2f} R"), delta_color="off", delta_arrow="off",
                help="Gains / pertes en R ; drawdown = pire baisse de la courbe en R")
    if stats["courbe"]:
        st.plotly_chart(r_curve_chart(stats["courbe"], "Courbe de résultats cumulés", "R"),
                        width="stretch")
    if stats["ambigus"]:
        st.caption(f"⚠️ {stats['ambigus']} décision(s) avec une ambiguïté de rejeu (détail dans "
                   "le journal) : le résultat retenu est l'hypothèse prudente.")

    cols = st.columns(3)
    for col, key, title in ((cols[0], "par_actif", "Par actif"), (cols[1], "par_style", "Par style"),
                            (cols[2], "par_mode", "Par mode")):
        rows = breakdown_rows(stats[key])
        if rows:
            col.markdown(f"**{title}**")
            col.dataframe(rows, hide_index=True)

    st.subheader("🎚️ Calibration de la confiance")
    st.caption("Une confiance de 80 % devrait correspondre à beaucoup plus de trades gagnants "
               "qu'une confiance de 50 %. Sinon, la confiance annoncée n'est pas fiable.")
    if stats["calibration"]:
        st.dataframe([{"Confiance annoncée": k, "Trades": v["trades_clotures"],
                       "Gagnants": fmt(v["taux_gagnants"], "{:.0f}%"),
                       "R moyen": fmt(v["r_moyen"], "{:+.2f}")}
                      for k, v in stats["calibration"].items()], hide_index=True)
    else:
        st.write("Pas encore de trade clôturé.")

    st.subheader("Fiabilité des agents")
    st.caption("Sur les décisions dont l'horizon est écoulé : le signal allait-il dans le sens "
               "du marché ? (signaux neutres exclus)")
    if stats["justesse_agents"]:
        st.dataframe(
            [{"Agent": AGENT_LABELS.get(name, name), "Signaux": v["signaux"], "Justes": v["justes"],
              "Taux de justesse": f"{v['taux']:.0f}%"}
             for name, v in stats["justesse_agents"].items()],
            hide_index=True, width="stretch",
        )
    else:
        st.write("Pas encore assez de décisions arrivées à échéance.")

    st.subheader("Journal")
    rows = [
        {
            "N°": r.id,
            "Date (UTC)": r.created_at.strftime("%Y-%m-%d %H:%M"),
            "Actif": r.asset,
            "Style": r.style.split(" (")[0],
            "Mode": "🎯 SMC" if r.mode == "smc" else "standard",
            "Décision": r.decision,
            "Confiance": fmt(r.confidence, "{}%"),
            "Prix": fmt(r.price),
            "Entrée": fmt(r.entry),
            "Stop": fmt(r.stop),
            "Objectif 1": fmt(r.targets[0] if r.targets else None),
            "Résultat": STATUS_ICON.get(o.status, o.status) + (" ⚠️" if o.ambiguities else "")
                        + (" 🔒" if o.frozen else ""),
            "R": fmt(o.r_multiple, "{:+.2f}"),
            "Variation prix": fmt(o.price_move_pct, "{:+.2f}%"),
            "Données": o.data_note,
            **{f"{s.icon} {s.key}": r.signals.get(s.key) or "—" for s in agents.DECISION_AGENTS},
        }
        for r, o in pairs
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("🔒 résultat figé (période entièrement couverte par les données) · ⚠️ ambiguïté de "
               "rejeu, hypothèse prudente retenue")
    st.download_button(
        "⬇️ Exporter en CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
        file_name="teamtrader_historique.csv", mime="text/csv",
    )

    st.subheader("🔍 Rouvrir une analyse")
    by_id = {r.id: (r, o) for r, o in pairs}
    rid = st.selectbox("Décision", list(by_id), format_func=lambda i: (
        f"N° {i} — {by_id[i][0].created_at:%Y-%m-%d %H:%M} — {by_id[i][0].asset} — "
        f"{by_id[i][0].decision}"))
    render_analysis_detail(*by_id[rid])
    with st.expander("Supprimer une décision"):
        did = st.selectbox("Décision à supprimer", list(by_id), format_func=lambda i: f"N° {i}",
                           key="delete_id")
        if st.button("Supprimer", type="secondary"):
            history.delete(con, did)
            st.rerun()
    st.caption(
        "Méthode v2 : rejeu sur bougies 15 min quand elles sont disponibles (sinon 1 h), avec "
        "la source et l'écart future/spot enregistrés au moment de la décision. La bougie de la "
        "décision est prise en compte et signalée ; les bougies encore ouvertes sont ignorées ; "
        "une période n'est terminée que si les données la couvrent. Stop et objectif dans la "
        "même bougie = perte ; objectif touché dans la bougie d'entrée = non compté.")
    render_costs()
    render_maintenance()


def render_costs():
    st.subheader("💰 Coûts IA")
    con = history.connect()
    allowed, spent = costs.budget_status(con)
    budget = costs.MONTHLY_BUDGET_USD
    st.metric("Dépense estimée ce mois-ci", f"{spent:.2f} $",
              f"budget {budget:.0f} $" if budget else "pas de budget défini", delta_color="off", delta_arrow="off")
    rows = costs.summary_by_kind(con)
    if rows:
        st.dataframe([{"Mois": m, "Type": k, "Appels": n, "Recherches web": w or 0,
                       "Coût estimé": f"{c:.2f} $"} for m, k, n, w, c in rows], hide_index=True)
    st.caption("Estimation à partir des tokens et recherches web renvoyés par l'API et du "
               "tableau de prix de teamtrader/costs.py. Budget mensuel : "
               "TEAMTRADER_MONTHLY_BUDGET_USD (au-delà, les analyses sont bloquées).")


def render_maintenance():
    with st.expander("🗄️ Sauvegarde et restauration de la base"):
        path = config.HISTORY_DB
        if os.path.exists(path):
            with open(path, "rb") as f:
                st.download_button("⬇️ Télécharger une sauvegarde (.db)", f.read(),
                                   file_name=f"teamtrader_{datetime.now():%Y%m%d_%H%M}.db",
                                   mime="application/octet-stream")
        up = st.file_uploader("Restaurer une sauvegarde", type=["db"])
        if up is not None and st.button("Remplacer la base actuelle par ce fichier"):
            ok, msg = backup.restore(up.getvalue(), path)
            (st.success if ok else st.error)(msg)
        st.caption("Sur Streamlit Community Cloud, le disque est effacé à chaque redémarrage : "
                   "télécharge une sauvegarde régulièrement.")


# ------------------------------------------------------------------------------ paper trading

PROFILE_LABELS = {
    "contract_size": "Taille du contrat (unités par lot)", "min_lot": "Lot minimum",
    "lot_step": "Pas de quantité (lot)", "spread": "Spread (en prix)",
    "slippage": "Glissement moyen (en prix)", "commission_per_lot": "Commission par lot ($ A/R)",
    "commission_pct": "Commission (% du nominal A/R)",
    "swap_long_pct": "Financement position longue (%/an)",
    "swap_short_pct": "Financement position courte (%/an)", "margin_pct": "Marge exigée (%)",
}


def render_risk_settings(con, settings: risk.RiskSettings):
    with st.expander("⚙️ Compte, limites de risque et profil broker"):
        with st.form("risk_settings"):
            c = st.columns(5)
            capital = c[0].number_input("Capital de départ ($)", min_value=100.0,
                                        value=float(settings.capital), step=1000.0)
            risk_pct = c[1].number_input("Risque par trade (%)", 0.1, 10.0,
                                         float(settings.risk_pct), 0.25)
            lev = c[2].number_input("Levier max (×)", 1.0, 100.0, float(settings.max_leverage), 1.0)
            open_risk = c[3].number_input("Risque cumulé max (%)", 0.5, 30.0,
                                          float(settings.max_open_risk_pct), 0.5)
            daily = c[4].number_input("Perte journalière max (%)", 0.5, 30.0,
                                      float(settings.daily_loss_limit_pct), 0.5)
            new_profiles = {}
            for key, prof in settings.profiles.items():
                st.markdown(f"**{ASSETS[key].name}**")
                cols = st.columns(5)
                values = {}
                for i, (name, label) in enumerate(PROFILE_LABELS.items()):
                    values[name] = cols[i % 5].number_input(
                        label, value=float(getattr(prof, name)), key=f"{key}_{name}",
                        format="%.4f" if name in ("lot_step", "min_lot", "commission_pct") else "%.2f")
                new_profiles[key] = risk.InstrumentProfile(**values)
            if st.form_submit_button("Enregistrer"):
                risk.save_settings(con, risk.RiskSettings(capital, risk_pct, lev, open_risk, daily,
                                                          new_profiles))
                st.success("Réglages enregistrés.")
                st.rerun()
        st.caption("Les valeurs par défaut sont des ordres de grandeur : remplace-les par celles "
                   "de ton broker (spread réel, commission, financement, taille de contrat).")


def render_paper():
    con = paper.connect()
    settings = risk.load_settings(con)
    render_risk_settings(con, settings)
    if st.button("🔄 Mettre à jour les positions"):
        _raw_candles.clear()
    problems = paper.update_all(con, fetch_raw)
    for p in problems:
        st.warning(f"Mise à jour impossible : {p}")
    positions = paper.load(con)
    acc = paper.account(positions, settings)
    perf = paper.performance(positions, settings.capital)

    c = st.columns(6)
    c[0].metric("Fonds propres", f"{acc.equity_live:,.0f} $",
                f"{acc.equity_live - settings.capital:+,.0f} $")
    c[1].metric("P&L réalisé (net)", f"{acc.realized:+,.0f} $")
    c[2].metric("P&L latent", f"{acc.unrealized:+,.0f} $")
    limit = settings.daily_loss_limit_pct / 100 * acc.equity
    c[3].metric("Aujourd'hui", f"{acc.today:+,.0f} $", f"limite −{limit:,.0f} $",
                delta_color="off", delta_arrow="off")
    c[4].metric("Risque ouvert", f"{acc.open_risk / acc.equity * 100:.2f} %",
                f"max {settings.max_open_risk_pct:.1f} %", delta_color="off", delta_arrow="off")
    c[5].metric("Marge utilisée", f"{acc.margin_used:,.0f} $")
    if acc.today <= -limit:
        st.error("⛔ Limite de perte journalière atteinte : aucune nouvelle position aujourd'hui.")

    open_pos = [p for p in positions if p.is_open]
    st.subheader("Positions ouvertes et ordres en attente")
    if not open_pos:
        st.info("Aucune position. Ouvre un ordre fictif depuis une décision (onglet Analyse).")
    else:
        st.dataframe([{
            "N°": p.id, "Actif": p.asset, "Sens": "ACHAT" if p.long else "VENTE",
            "Lots": p.lots, "Statut": p.status, "Entrée": p.entry, "Stop": p.stop,
            "Objectif": fmt(p.target), "Dernier cours": fmt(p.exit_price) if p.status == "ouverte" else "—",
            "Latent (net)": fmt(paper.unrealized(p), "{:+,.2f} $") if p.status == "ouverte" else "—",
            "Échéance (UTC)": f"{p.expires_at:%Y-%m-%d %H:%M}", "Remarques": p.notes or "",
        } for p in open_pos], hide_index=True, width="stretch")
        cols = st.columns([2, 1, 1])
        pid = cols[0].selectbox("Position", [p.id for p in open_pos],
                                format_func=lambda i: f"N° {i}")
        pos = next(p for p in open_pos if p.id == pid)
        if cols[1].button("Clôturer au dernier cours" if pos.status == "ouverte" else "Annuler l'ordre"):
            paper.close_at_market(con, pos, pos.exit_price or pos.entry)
            st.rerun()

    closed = [p for p in positions if not p.is_open]
    st.subheader("📈 Performance du compte fictif")
    c = st.columns(5)
    c[0].metric("Trades clôturés", perf["trades"], f"{perf['gagnants']} gagnants", delta_color="off", delta_arrow="off")
    c[1].metric("P&L net", f"{perf['pnl_net']:+,.0f} $")
    c[2].metric("Coûts payés", f"{perf['couts']:,.0f} $")
    c[3].metric("Profit factor", fmt(perf["profit_factor"], "{:.2f}"))
    c[4].metric("Drawdown max", fmt(perf["drawdown_max_pct"], "{:.1f} %"))
    if perf["courbe"]:
        st.plotly_chart(r_curve_chart(perf["courbe"], "Courbe de capital (net de coûts)", "$"),
                        width="stretch")
    if closed:
        st.dataframe([{
            "N°": p.id, "Actif": p.asset, "Sens": "ACHAT" if p.long else "VENTE", "Lots": p.lots,
            "Statut": p.status, "Sortie": p.exit_reason or "", "Entrée": p.entry,
            "Prix de sortie": fmt(p.exit_price), "Brut": fmt(p.gross_pnl, "{:+,.2f} $"),
            "Coûts": fmt(p.costs, "{:,.2f} $"), "Net": fmt(p.net_pnl, "{:+,.2f} $"),
            "Clôture": (p.closed_at or "")[:16].replace("T", " "), "Remarques": p.notes or "",
        } for p in closed], hide_index=True, width="stretch")
    st.caption("Exécutions simulées sur les bougies 15 min / 1 h avec le moteur de rejeu de "
               "l'historique : spread et commission sur chaque trade, glissement sur les sorties "
               "au stop ou au marché, financement par nuit de détention.")


def run_analysis(api_key: str, asset_key: str, style: str, notes: str, smc_mode: bool) -> None:
    if not api_key:
        st.error("Renseigne ta clé API Anthropic (variable ANTHROPIC_API_KEY ou champ à gauche).")
        return
    con = history.connect()
    allowed, spent = costs.budget_status(con)
    if not allowed:
        st.error(f"⛔ Budget IA du mois atteint ({spent:.2f} $ / {costs.MONTHLY_BUDGET_USD:.0f} $) : "
                 "analyse bloquée. Augmente TEAMTRADER_MONTHLY_BUDGET_USD si besoin.")
        return
    client = anthropic.Anthropic(api_key=api_key)
    st.session_state.pop("result", None)
    started = time.monotonic()
    log.info("Analyse %s (%s, SMC=%s) démarrée", asset_key, style, smc_mode)

    with st.status("Analyse en cours…", expanded=True) as status:
        st.write("📥 Récupération des données de marché…")
        try:
            portfolio = paper.portfolio_text(paper.load(paper.connect()))
            ctx = pipeline.prepare(asset_key, style, notes, smc_mode=smc_mode, portfolio=portfolio)
        except Exception as e:
            log.exception("Échec de la récupération des données")
            status.update(label="Échec de la récupération des données", state="error")
            st.error(f"Impossible de récupérer les données de marché : {e}")
            return
        degraded = [i for i in ctx.data_items if i.status != quality.OK]
        if degraded:
            st.write("⚠️ Données manquantes ou périmées : "
                     + ", ".join(f"{i.name} ({i.status})" for i in degraded))
        if ctx.smc_map:
            st.write(f"🎯 Setup SMC ({ctx.smc_map.timeframe}) : **{ctx.smc_map.setup.status}**")
        st.write(f"Prix actuel : **{ctx.price:,.2f}** — les {len(ctx.agent_specs)} "
                 "analystes travaillent en parallèle…")

        reports = []
        for r in pipeline.run_specialists(client, ctx):
            reports.append(r)
            verdict = r.error or DIRECTION_BADGE.get(r.direction or "", "terminé")
            st.write(f"{r.icon} {r.name} : {verdict} ({r.duration_s:.0f} s)")

        st.write("🧠 Le chef de desk délibère…")
        try:
            forecast = st.session_state.get("calendar", (None, None, None))[1]
            result = pipeline.decide(
                client, ctx, reports,
                calendar_forecast=calendar_summary(forecast) if forecast else "",
            )
        except Exception as e:
            log.exception("Échec de la décision")
            status.update(label="Échec de la décision", state="error")
            st.error(str(e))
            costs.record(con, "analyse", [u for r in reports for u in r.usage])
            return

        duration = time.monotonic() - started
        record_id = None
        try:
            analysis = pipeline.analysis_record(ctx, reports, result, round(duration, 1))
            costs.record(con, "analyse", analysis["consommation"])
            record_id = history.save(
                con, asset=asset_key, style=style, price=ctx.price, decision=result.decision,
                reports=reports, source=ctx.source, mode="smc" if ctx.smc_mode else "standard",
                provider=ctx.provider, basis=ctx.basis, analysis=analysis)
        except Exception as e:
            log.exception("Enregistrement impossible")
            st.warning(f"Décision non enregistrée dans l'historique : {e}")
        log.info("Analyse %s terminée en %.0fs : %s (%s)", asset_key, duration,
                 result.decision.decision, result.status)
        status.update(label=f"Analyse terminée en {duration:.0f} s", state="complete",
                      expanded=False)
        st.session_state["result"] = (ctx, reports, result, record_id)


# ------------------------------------------------------------------------------ page

password = os.environ.get("TEAMTRADER_PASSWORD", "")
if password and not st.session_state.get("authenticated"):
    st.title("📈 TeamTrader")
    typed = st.text_input("Mot de passe", type="password")
    if typed and hmac.compare_digest(typed, password):
        st.session_state["authenticated"] = True
        st.rerun()
    if typed:
        st.error("Mot de passe incorrect.")
    st.stop()

st.title("📈 TeamTrader")
st.caption(
    "Des agents IA analysent le marché (fondamental, technique, géopolitique, sentiment, "
    "et Smart Money en option), puis un agent général prend la décision. Chaque décision est "
    "contrôlée, dimensionnée avec les coûts réels et mesurée dans le temps."
)

with st.sidebar:
    st.header("Paramètres")
    asset_key = st.radio(
        "Actif", list(ASSETS), format_func=lambda k: ASSETS[k].name, horizontal=True
    )
    style = st.selectbox("Style de trading", list(TRADING_STYLES))
    smc_mode = st.toggle(
        "🎯 Mode Smart Money Concepts",
        help="Ajoute l'analyste SMC (structure, order blocks, FVG, liquidité). "
             "Une entrée n'est validée qu'après le balayage de l'inducement : sinon la "
             "décision est automatiquement ATTENDRE.",
    )
    notes = st.text_area("Remarques pour l'équipe (optionnel)",
                         placeholder="Ex : je pense que la Fed va surprendre…")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.text_input(
        "Clé API Anthropic", type="password"
    )
    run = st.button("🚀 Lancer l'analyse", type="primary", width="stretch")
    _, month_spent = costs.budget_status(history.connect())
    budget_txt = f" / {costs.MONTHLY_BUDGET_USD:.0f} $" if costs.MONTHLY_BUDGET_USD else ""
    st.caption(f"Modèle : `{config.MODEL}` · dépense IA du mois : {month_spent:.2f} ${budget_txt}")
    st.caption("Capital, risque par trade et profil broker : onglet 💼 Paper trading.")

tab_analysis, tab_calendar, tab_smc, tab_paper, tab_history = st.tabs(
    ["🔎 Analyse", "📅 Calendrier US", "🎯 Scanner SMC", "💼 Paper trading",
     "📒 Historique et performance"]
)

with tab_analysis:
    if run:
        run_analysis(api_key, asset_key, style, notes, smc_mode)

    if "result" in st.session_state:
        ctx, reports, result, record_id = st.session_state["result"]
        fig = price_chart(ctx)
        add_levels(fig, result.decision)
        render_decision(ctx, result, record_id)
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Source des prix : {ctx.source}")
        if ctx.smc_map:
            st.subheader("🎯 Lecture Smart Money")
            render_smc(ctx.smc_map, ctx.frames[ctx.smc_map.timeframe], ctx.asset.name)
        st.subheader("Rapports détaillés")
        order = {s.key: i for i, s in enumerate(agents.DECISION_AGENTS)}
        for r in sorted(reports, key=lambda r: order.get(r.key, 99)):
            render_report(r)
    else:
        st.info("Choisis un actif à gauche puis clique sur **Lancer l'analyse**.")

with tab_calendar:
    render_calendar(api_key)
    render_forecast_tracking(api_key)

with tab_smc:
    render_smc_scanner(asset_key, style)

with tab_paper:
    render_paper()

with tab_history:
    render_history()

st.divider()
st.caption(
    "⚠️ Outil d'aide à la décision uniquement — pas un conseil en investissement. "
    "Le trading comporte un risque de perte en capital ; vérifie toujours les niveaux "
    "sur ta plateforme avant de passer un ordre."
)
