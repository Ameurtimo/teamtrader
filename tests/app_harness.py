"""Harnais de test de l'interface : lance app.py avec des données et un modèle simulés.

Aucun appel réseau ; le vrai pipeline de décision (seuil de qualité, contrôles, règle SMC,
dimensionnement, paper trading) s'exécute. Utilisé par tests/test_app_smoke.py et
lançable à la main : streamlit run tests/app_harness.py
"""
import os, sys, runpy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ["ANTHROPIC_API_KEY"] = "fake"
from test_teamtrader import make_ohlcv, FakeClient
from teamtrader import pipeline, data, agents, history, economic_calendar, paper, risk, costs, config
config.HISTORY_DB = os.environ.get("TEAMTRADER_TEST_DB", os.path.join(ROOT, "harness.db"))
costs.MONTHLY_BUDGET_USD = 50

NOW = datetime.now(timezone.utc)
H = make_ohlcv(n=1500, drift=0.6)
H.index = H.index + (NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1) - H.index[-1])
M15 = make_ohlcv(n=3000, drift=0.15, freq="15min")
M15.index = M15.index + (NOW.replace(second=0, microsecond=0) - timedelta(minutes=30) - M15.index[-1])
BASIS = 12.0

def get_price_history(asset, start=None, with_15m=False):
    f = {"1h": H, "4h": data.resample(H, "4h"), "1d": data.resample(H, "1D")}
    if with_15m: f = {"15m": M15, **f}
    return data.PriceData(f if start is None else {"1h": H}, "simulation (Yahoo recalé, écart +12)", "yahoo", BASIS)
def get_candles(asset, provider, start, tf):
    df = M15 if tf == "15m" else H
    raw = df[df.index >= start].copy(); raw[["Open", "High", "Low", "Close"]] += BASIS
    return raw
data.get_price_history, data.get_candles = get_price_history, get_candles
data.current_basis = lambda asset: BASIS
data.macro_snapshot = lambda: [{"nom": n, "ticker": t, "dernier": 1.0, "var_1j_%": 0.1, "var_5j_%": 0.2, "var_1m_%": 0.3, "date": NOW - timedelta(days=1)} for n, t in list(data.MACRO_TICKERS.items())]
data.crypto_fear_greed = lambda: [{"date": (NOW - timedelta(days=i)).date().isoformat(), "valeur": 30 + i, "classe": "Fear"} for i in range(31)]
data.crypto_derivatives = lambda: {"funding_pct": 0.01, "open_interest_btc": 250000, "as_of": NOW - timedelta(hours=1), "source": "OKX BTC-USDT-SWAP"}
def _cot(): raise data.MarketDataError("CFTC injoignable (simulation)")
data.gold_cot = _cot
economic_calendar.fetch_us_events = lambda: [economic_calendar.EconomicEvent(NOW + timedelta(hours=h), t, "forte", f, pv) for h, t, f, pv in [(20, "CPI m/m", "0.3%", "0.4%"), (68, "Non-Farm Employment Change", "180K", "151K")]]

def usage(label):
    return {"label": label, "model": "claude-opus-5-5", "input_tokens": 12000, "output_tokens": 2500, "cache_read_tokens": 0, "cache_write_tokens": 0, "web_searches": 3, "cost_usd": 0.13}

def run_specialists(client, ctx, model=None):
    for s, d in zip(ctx.agent_specs, ["HAUSSIER", "HAUSSIER", "NEUTRE", "BAISSIER", "HAUSSIER"]):
        yield agents.AgentReport(s.key, s.name, s.icon, text=f"Analyse **{s.key}**...\n## Signal\n- Direction : {d}\n- Confiance : 70%", direction=d, confidence=70, sources=[{"title": "Reuters", "url": "https://reuters.com"}] if s.web_search else [], model="claude-opus-5-5", duration_s=41.0, usage=[usage(s.key)])
pipeline.run_specialists = run_specialists

def canned(p, first_invalid):
    mk = lambda tp: agents.TradingDecision(decision="ACHAT", confiance=68, horizon="3 à 5 jours", synthese="Technique et fondamental alignés à la hausse ; sentiment trop optimiste.", prix_entree=round(p, 2), zone_entree="Au marché", stop_loss=round(p - 25, 2), take_profits=tp, vues_agents=[agents.AgentView(agent="technique", direction="HAUSSIER", poids_dans_la_decision=40, commentaire="Tendance alignée")], arguments_pour=["Tendance alignée"], arguments_contre=["Sentiment extrême"], conditions_invalidation=["Clôture 4h sous le stop"], evenements_a_surveiller=["NFP vendredi"])
    resp = lambda d: NS(stop_reason="end_turn", parsed_output=d, content=[], model="claude-opus-5-5", usage=NS(input_tokens=20000, output_tokens=3000, cache_read_input_tokens=0, cache_creation_input_tokens=0, server_tool_use=None))
    good = mk([round(p + 60, 2), round(p + 110, 2)])
    return [resp(mk([round(p + 20, 2)])), resp(good)] if first_invalid else [resp(good)]
# Streamlit réexécute ce script à chaque interaction : on garde la VRAIE fonction une fois.
_real_decide = getattr(pipeline, "_original_decide", pipeline.decide)
pipeline._original_decide = _real_decide
def decide(client, ctx, reports, model=None, calendar_forecast=""):
    return _real_decide(FakeClient(canned(ctx.price, first_invalid=True)), ctx, reports, calendar_forecast=calendar_forecast)
pipeline.decide = decide

def forecast_calendar(client, text, track_record=""):
    evs = [agents.EventForecast(evenement="Non-Farm Employment Change", date_heure_utc=(NOW + timedelta(hours=68)).strftime("%Y-%m-%d %H:%M"), importance="forte", precedent="151K", consensus="180K", prevision="EN DESSOUS", estimation="140K", probabilite=62, indicateurs_avances=["ADP faible"], reaction_usd="BAISSIER", reaction_or="HAUSSIER", reaction_btc="HAUSSIER", explication="...")]
    return agents.CalendarForecast(synthese="Semaine dominée par le NFP.", evenements=evs, gestion_du_risque="Réduire la taille avant le NFP."), agents.AgentReport("calendrier", "Économiste", "📅", text="Note.", usage=[usage("calendrier")])
agents.forecast_calendar = forecast_calendar

# --- historique simulé : décisions passées (avec écart future/spot enregistré)
if not os.path.exists(config.HISTORY_DB):
    con = history.connect()
    for back, dec, conf in [(900, "ACHAT", 80), (700, "VENTE", 55), (500, "ACHAT", 72), (420, "ACHAT", 85), (300, "ATTENDRE", 40), (120, "ACHAT", 66), (6, "ACHAT", 60)]:
        ts = H.index[-back].to_pydatetime() + timedelta(minutes=20); p = float(H.Close.iloc[-back])
        sign = 1 if dec != "VENTE" else -1
        d = agents.TradingDecision(decision=dec, confiance=conf, horizon="", synthese="", prix_entree=p if dec != "ATTENDRE" else None, zone_entree="", stop_loss=p - sign * 25 if dec != "ATTENDRE" else None, take_profits=[p + sign * 45] if dec != "ATTENDRE" else [], vues_agents=[], arguments_pour=[], arguments_contre=[], conditions_invalidation=[], evenements_a_surveiller=[])
        reps = [agents.AgentReport(s.key, "", "", direction=x) for s, x in zip(agents.SPECIALISTS, ["HAUSSIER", "BAISSIER", "NEUTRE", "HAUSSIER"])]
        history.save(con, asset="XAUUSD", style="Swing (quelques jours)", price=p, decision=d, reports=reps, created_at=ts, provider="yahoo", basis=BASIS, analysis={"parametres": {"modele_demande": "claude-opus-5-5"}, "decision": {"statut": "valide"}, "rapports": [{"agent": "technique", "direction": "HAUSSIER", "texte": "Rapport archivé.", "sources": []}], "couts": {"cost_usd": 0.6}})
    costs.record(con, "analyse", [usage("ancienne")] * 5)
    pc = paper.connect(); st_ = risk.load_settings(pc)
    for back in (600, 200):
        p = float(H.Close.iloc[-back]); ts = H.index[-back].to_pydatetime()
        s = risk.size_position(asset="XAUUSD", long=True, entry=p, stop=p - 25, target=p + 45, settings=st_, equity=10000, open_positions=[], realized_today=0)
        paper.place_order(pc, asset="XAUUSD", long=True, sizing=s, entry=p, stop=p - 25, target=p + 45, price=p, max_hours=240, profile=st_.profiles["XAUUSD"], provider="yahoo", basis=BASIS, created_at=ts)

runpy.run_path(os.path.join(ROOT, "app.py"), run_name="__main__")
