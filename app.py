"""Interface web TeamTrader — lancer avec : streamlit run app.py"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta

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

from teamtrader import agents, config, data, history, indicators, pipeline  # noqa: E402
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


AGENT_LABELS = {s.key: f"{s.icon} {s.name}" for s in agents.SPECIALISTS}
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
    if st.button("🔄 Actualiser les prix"):
        candles_since.clear()
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
            "Décision": r.decision,
            "Confiance": fmt(r.confidence, "{}%"),
            "Prix": fmt(r.price),
            "Entrée": fmt(r.entry),
            "Stop": fmt(r.stop),
            "Objectif 1": fmt(r.targets[0] if r.targets else None),
            "Résultat": STATUS_ICON.get(o.status, o.status),
            "R": fmt(o.r_multiple, "{:+.2f}"),
            "Variation prix": fmt(o.price_move_pct, "{:+.2f}%"),
            **{f"{s.icon} {s.key}": r.signals.get(s.key) or "—" for s in agents.SPECIALISTS},
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


def run_analysis(api_key: str, asset_key: str, style: str, notes: str) -> None:
    if not api_key:
        st.error("Renseigne ta clé API Anthropic (variable ANTHROPIC_API_KEY ou champ à gauche).")
        return
    client = anthropic.Anthropic(api_key=api_key)
    st.session_state.pop("result", None)

    with st.status("Analyse en cours…", expanded=True) as status:
        st.write("📥 Récupération des données de marché…")
        try:
            ctx = pipeline.prepare(asset_key, TRADING_STYLES[style], notes)
        except Exception as e:
            status.update(label="Échec de la récupération des données", state="error")
            st.error(f"Impossible de récupérer les données de marché : {e}")
            return
        st.write(f"Prix actuel : **{ctx.price:,.2f}** — les {len(agents.SPECIALISTS)} "
                 "analystes travaillent en parallèle…")

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
        else:
            try:
                with history.connect() as con:
                    history.save(con, asset=asset_key, style=style, price=ctx.price,
                                 decision=decision, reports=reports, source=ctx.source)
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
    "Quatre agents IA analysent le marché (fondamental, technique, géopolitique, sentiment), "
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

tab_analysis, tab_history = st.tabs(["🔎 Analyse", "📒 Historique et fiabilité"])

with tab_analysis:
    if run:
        run_analysis(api_key, asset_key, style, notes)

    if "result" in st.session_state:
        ctx, reports, decision = st.session_state["result"]
        fig = price_chart(ctx)
        if decision:
            add_levels(fig, decision)
            render_decision(ctx, decision, capital, risk)
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Source des prix : {ctx.source}")
        st.subheader("Rapports détaillés")
        order = {s.key: i for i, s in enumerate(agents.SPECIALISTS)}
        for r in sorted(reports, key=lambda r: order[r.key]):
            render_report(r)
    else:
        st.info("Choisis un actif à gauche puis clique sur **Lancer l'analyse**.")

with tab_history:
    render_history()

st.divider()
st.caption(
    "⚠️ Outil d'aide à la décision uniquement — pas un conseil en investissement. "
    "Le trading comporte un risque de perte en capital ; vérifie toujours les niveaux "
    "sur ta plateforme avant de passer un ordre."
)
