"""Interface web TeamTrader — lancer avec : streamlit run app.py"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

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
    agents, config, data, economic_calendar, history, indicators, pipeline, smc,
)
from teamtrader.config import ASSETS, TRADING_STYLES  # noqa: E402

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
                s.poi.kind if s.poi else None, delta_color="off")
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
                        anthropic.Anthropic(api_key=api_key), calendar_text)
                except Exception as e:
                    forecast, report = None, agents.AgentReport(
                        key="calendrier", name="", icon="", error=str(e))
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


def render_decision(ctx: pipeline.Context, d: agents.TradingDecision, capital: float, risk: float):
    color = DECISION_COLOR[d.decision]
    st.markdown(
        f"<div style='padding:18px;border-radius:12px;background:{color};color:white'>"
        f"<div style='font-size:14px;opacity:.85'>Décision du chef de desk</div>"
        f"<div style='font-size:34px;font-weight:700'>{d.decision} · {ctx.asset.name}</div>"
        f"<div style='font-size:16px'>Confiance {d.confiance}% · Horizon : {d.horizon}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    st.markdown(d.synthese)

    tp1 = d.take_profits[0] if d.take_profits else None
    rr = pipeline.risk_reward(d.prix_entree, d.stop_loss, tp1)
    c = st.columns(5)
    c[0].metric("Prix actuel", f"{ctx.price:,.2f}")
    c[1].metric("Entrée", f"{d.prix_entree:,.2f}" if d.prix_entree else "—")
    c[2].metric("Stop-loss", f"{d.stop_loss:,.2f}" if d.stop_loss else "—")
    c[3].metric("Objectif 1", f"{tp1:,.2f}" if tp1 else "—")
    c[4].metric("Risque / rendement (TP1)", f"{rr:.2f}" if rr else "—")
    targets = " → ".join(f"{t:,.2f}" for t in d.take_profits) or "—"
    st.caption(f"Entrée : {d.zone_entree} · Objectifs : {targets}")

    size = pipeline.position_size(capital, risk, d.prix_entree, d.stop_loss)
    if size and d.decision != "ATTENDRE":
        st.info(
            f"**Taille de position** pour risquer {risk:.1f}% de {capital:,.0f} USD "
            f"({size['montant_risque']:,.0f} USD) : **{size['unites']:.4f} unités** "
            f"≈ {size['valeur_position']:,.0f} USD (levier implicite ×{size['levier_implicite']:.2f})."
        )

    st.subheader("Avis des analystes")
    st.dataframe(
        [
            {
                "Agent": v.agent,
                "Direction": DIRECTION_BADGE[v.direction],
                "Poids": f"{v.poids_dans_la_decision}%",
                "Ce que le chef en retient": v.commentaire,
            }
            for v in d.vues_agents
        ],
        hide_index=True,
        width="stretch",
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


# ------------------------------------------------------------------------------ historique

STATUS_ICON = {
    "gagnant": "✅ gagnant", "perdant": "❌ perdant", "expiré": "⏱️ expiré",
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


def evaluate_all(records: list[history.Record]):
    pairs = []
    for asset_key in sorted({r.asset for r in records}):
        recs = [r for r in records if r.asset == asset_key]
        start = min(r.created_at for r in recs) - timedelta(hours=2)
        try:
            candles = candles_since(asset_key, start.replace(minute=0, second=0, microsecond=0)
                                    .isoformat())
        except Exception as e:
            st.warning(f"Prix indisponibles pour {ASSETS[asset_key].name} : {e}")
            candles = pd.DataFrame(columns=data.OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        pairs += [(r, history.evaluate(r, candles)) for r in recs]
    return sorted(pairs, key=lambda p: p[0].created_at, reverse=True)


def fmt(v, pattern="{:,.2f}"):
    return "—" if v is None else pattern.format(v)


def render_history():
    con = history.connect()
    records = history.load(con)
    if not records:
        st.info("Aucune décision enregistrée pour l'instant : chaque analyse terminée "
                "est ajoutée automatiquement à l'historique.")
        return
    top = st.columns([1, 2])
    if top[0].button("🔄 Actualiser les prix"):
        candles_since.clear()
    mode = top[1].radio("Décisions", ["Toutes", "Standard", "SMC"], horizontal=True,
                        label_visibility="collapsed")
    if mode != "Toutes":
        records = [r for r in records if r.mode == mode.lower()]
        if not records:
            st.info(f"Aucune décision en mode {mode}.")
            return
    pairs = evaluate_all(records)
    stats = history.statistics(pairs)

    c = st.columns(5)
    c[0].metric("Décisions", stats["decisions"])
    c[1].metric("Trades clôturés", stats["trades_clotures"])
    c[2].metric("Taux de réussite", fmt(stats["taux_reussite"], "{:.0f}%"),
                help="Gagnants / (gagnants + perdants)")
    c[3].metric("R moyen", fmt(stats["r_moyen"], "{:+.2f}"),
                help="Gain moyen exprimé en multiple du risque pris (1R = distance au stop)")
    c[4].metric("R cumulé", fmt(stats["r_total"], "{:+.2f}"))

    st.subheader("Fiabilité des agents")
    st.caption("Sur les décisions dont l'horizon est écoulé : le signal allait-il dans le sens "
               "du marché ? (signaux neutres exclus)")
    if stats["justesse_agents"]:
        st.dataframe(
            [
                {"Agent": AGENT_LABELS.get(name, name), "Signaux": v["signaux"],
                 "Justes": v["justes"],
                 "Taux de justesse": f"{v['taux']:.0f}%"}
                for name, v in stats["justesse_agents"].items()
            ],
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
            "Résultat": STATUS_ICON.get(o.status, o.status),
            "R": fmt(o.r_multiple, "{:+.2f}"),
            "Variation prix": fmt(o.price_move_pct, "{:+.2f}%"),
            **{f"{s.icon} {s.key}": r.signals.get(s.key) or "—" for s in agents.DECISION_AGENTS},
        }
        for r, o in pairs
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.download_button(
        "⬇️ Exporter en CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
        file_name="teamtrader_historique.csv", mime="text/csv",
    )
    with st.expander("Supprimer une décision"):
        rid = st.selectbox("Décision", [r.id for r in records],
                           format_func=lambda i: f"N° {i}")
        if st.button("Supprimer", type="secondary"):
            history.delete(con, rid)
            st.rerun()
    st.caption("Méthode : l'ordre est déclenché quand une bougie 1h touche le prix d'entrée "
               "(immédiatement si l'entrée est au prix du marché), puis on regarde si "
               "l'objectif 1 ou le stop est touché en premier. Si une même bougie touche "
               "les deux, on compte une perte.")


def run_analysis(api_key: str, asset_key: str, style: str, notes: str, smc_mode: bool) -> None:
    if not api_key:
        st.error("Renseigne ta clé API Anthropic (variable ANTHROPIC_API_KEY ou champ à gauche).")
        return
    client = anthropic.Anthropic(api_key=api_key)
    st.session_state.pop("result", None)

    with st.status("Analyse en cours…", expanded=True) as status:
        st.write("📥 Récupération des données de marché…")
        try:
            ctx = pipeline.prepare(asset_key, style, notes, smc_mode=smc_mode)
        except Exception as e:
            status.update(label="Échec de la récupération des données", state="error")
            st.error(f"Impossible de récupérer les données de marché : {e}")
            return
        if ctx.smc_map:
            st.write(f"🎯 Setup SMC ({ctx.smc_map.timeframe}) : **{ctx.smc_map.setup.status}**")
        st.write(f"Prix actuel : **{ctx.price:,.2f}** — les {len(ctx.agent_specs)} "
                 "analystes travaillent en parallèle…")

        reports = []
        for r in pipeline.run_specialists(client, ctx):
            reports.append(r)
            verdict = r.error or DIRECTION_BADGE.get(r.direction or "", "terminé")
            st.write(f"{r.icon} {r.name} : {verdict}")

        st.write("🧠 Le chef de desk délibère…")
        try:
            forecast = st.session_state.get("calendar", (None, None, None))[1]
            decision = pipeline.decide(
                client, ctx, reports,
                calendar_forecast=calendar_summary(forecast) if forecast else "",
            )
        except Exception as e:
            status.update(label="Échec de la décision", state="error")
            st.error(str(e))
            st.session_state["result"] = (ctx, reports, None)
        else:
            try:
                with history.connect() as con:
                    history.save(con, asset=asset_key, style=style, price=ctx.price,
                                 decision=decision, reports=reports, source=ctx.source,
                                 mode="smc" if ctx.smc_map else "standard")
            except Exception as e:
                st.warning(f"Décision non enregistrée dans l'historique : {e}")
            status.update(label="Analyse terminée", state="complete", expanded=False)
            st.session_state["result"] = (ctx, reports, decision)


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
    "et Smart Money en option), puis un agent général prend la décision."
)

with st.sidebar:
    st.header("Paramètres")
    asset_key = st.radio(
        "Actif", list(ASSETS), format_func=lambda k: ASSETS[k].name, horizontal=True
    )
    style = st.selectbox("Style de trading", list(TRADING_STYLES))
    capital = st.number_input("Capital (USD)", min_value=0.0, value=10_000.0, step=1_000.0)
    risk = st.slider("Risque par trade (% du capital)", 0.25, 5.0, 1.0, 0.25)
    smc_mode = st.toggle(
        "🎯 Mode Smart Money Concepts",
        help="Ajoute l'analyste SMC (structure, order blocks, FVG, liquidité). "
             "Une entrée n'est validée qu'après le balayage de l'inducement : sinon la "
             "décision est automatiquement ATTENDRE.",
    )
    notes = st.text_area("Remarques pour l'équipe (optionnel)",
                         placeholder="Ex : je suis déjà long depuis 2650…")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.text_input(
        "Clé API Anthropic", type="password"
    )
    run = st.button("🚀 Lancer l'analyse", type="primary", width="stretch")
    st.caption(f"Modèle : `{config.MODEL}`")

tab_analysis, tab_calendar, tab_smc, tab_history = st.tabs(
    ["🔎 Analyse", "📅 Calendrier US", "🎯 Scanner SMC", "📒 Historique et fiabilité"]
)

with tab_analysis:
    if run:
        run_analysis(api_key, asset_key, style, notes, smc_mode)

    if "result" in st.session_state:
        ctx, reports, decision = st.session_state["result"]
        fig = price_chart(ctx)
        if decision:
            add_levels(fig, decision)
            render_decision(ctx, decision, capital, risk)
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

with tab_smc:
    render_smc_scanner(asset_key, style)

with tab_history:
    render_history()

st.divider()
st.caption(
    "⚠️ Outil d'aide à la décision uniquement — pas un conseil en investissement. "
    "Le trading comporte un risque de perte en capital ; vérifie toujours les niveaux "
    "sur ta plateforme avant de passer un ordre."
)
