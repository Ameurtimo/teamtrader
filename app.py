"""Interface web TeamTrader — lancer avec : streamlit run app.py"""

from __future__ import annotations

import os

import anthropic
import plotly.graph_objects as go
import streamlit as st

from teamtrader import agents, config, indicators, pipeline
from teamtrader.config import ASSETS, TRADING_STYLES

st.set_page_config(page_title="TeamTrader", page_icon="📈", layout="wide")

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


# ------------------------------------------------------------------------------ page

st.title("📈 TeamTrader")
st.caption(
    "Trois agents IA analysent le marché (fondamental, technique, géopolitique), "
    "puis un agent général prend la décision."
)

with st.sidebar:
    st.header("Paramètres")
    asset_key = st.radio(
        "Actif", list(ASSETS), format_func=lambda k: ASSETS[k].name, horizontal=True
    )
    style = st.selectbox("Style de trading", list(TRADING_STYLES))
    capital = st.number_input("Capital (USD)", min_value=0.0, value=10_000.0, step=1_000.0)
    risk = st.slider("Risque par trade (% du capital)", 0.25, 5.0, 1.0, 0.25)
    notes = st.text_area("Remarques pour l'équipe (optionnel)",
                         placeholder="Ex : je suis déjà long depuis 2650…")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.text_input(
        "Clé API Anthropic", type="password"
    )
    run = st.button("🚀 Lancer l'analyse", type="primary", width="stretch")
    st.caption(f"Modèle : `{config.MODEL}`")

if run:
    if not api_key:
        st.error("Renseigne ta clé API Anthropic (variable ANTHROPIC_API_KEY ou champ à gauche).")
        st.stop()
    client = anthropic.Anthropic(api_key=api_key)
    st.session_state.pop("result", None)

    with st.status("Analyse en cours…", expanded=True) as status:
        st.write("📥 Récupération des données de marché…")
        try:
            ctx = pipeline.prepare(asset_key, TRADING_STYLES[style], notes)
        except Exception as e:
            status.update(label="Échec de la récupération des données", state="error")
            st.error(f"Impossible de récupérer les données de marché : {e}")
            st.stop()
        st.write(f"Prix actuel : **{ctx.price:,.2f}** — les 3 analystes travaillent en parallèle…")

        reports = []
        for r in pipeline.run_specialists(client, ctx):
            reports.append(r)
            verdict = r.error or DIRECTION_BADGE.get(r.direction or "", "terminé")
            st.write(f"{r.icon} {r.name} : {verdict}")

        st.write("🧠 Le chef de desk délibère…")
        try:
            decision = pipeline.decide(client, ctx, reports)
        except Exception as e:
            status.update(label="Échec de la décision", state="error")
            st.error(str(e))
            st.session_state["result"] = (ctx, reports, None)
            decision = None
        else:
            status.update(label="Analyse terminée", state="complete", expanded=False)
            st.session_state["result"] = (ctx, reports, decision)

if "result" in st.session_state:
    ctx, reports, decision = st.session_state["result"]
    fig = price_chart(ctx)
    if decision:
        add_levels(fig, decision)
        render_decision(ctx, decision, capital, risk)
    st.plotly_chart(fig, width="stretch")
    st.subheader("Rapports détaillés")
    order = {s.key: i for i, s in enumerate(agents.SPECIALISTS)}
    for r in sorted(reports, key=lambda r: order[r.key]):
        render_report(r)
    if ctx.asset.key == "XAUUSD":
        st.caption("Prix de l'or : contrat à terme COMEX (GC=F), légèrement au-dessus du spot XAU/USD.")
else:
    st.info("Choisis un actif à gauche puis clique sur **Lancer l'analyse**.")

st.divider()
st.caption(
    "⚠️ Outil d'aide à la décision uniquement — pas un conseil en investissement. "
    "Le trading comporte un risque de perte en capital ; vérifie toujours les niveaux "
    "sur ta plateforme avant de passer un ordre."
)
