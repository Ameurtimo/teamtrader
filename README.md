# 📈 TeamTrader

Application d'aide à la décision de trading sur **l'or (XAU/USD)** et **le bitcoin (BTC/USD)**,
construite comme une équipe d'agents IA (Claude) :

```
                ┌──────────────────────┐
                │  Données de marché   │  Yahoo Finance : bougies 1h / 4h / 1j,
                │  + indicateurs       │  DXY, taux US, VIX, Nasdaq, pétrole…
                └──────────┬───────────┘
        ┌──────────────────┼──────────────────┐        (en parallèle)
        ▼                  ▼                  ▼
 📊 Fondamental      📈 Technique        🌍 Géopolitique
 macro, Fed, flux    EMA, RSI, MACD,     actualité, conflits,
 ETF, on-chain       ATR, supports…      sanctions, élections
 (+ recherche web)   (chiffres calculés) (+ recherche web)
        └──────────────────┼──────────────────┘
                           ▼
                🧠 Chef de desk (agent général)
       lit les 3 rapports → ACHAT / VENTE / ATTENDRE
       entrée, stop-loss, objectifs, confiance, invalidation
```

- Chaque spécialiste rédige un rapport et termine par un **signal** (haussier / baissier / neutre + confiance).
- Le chef de desk pondère les avis selon l'horizon choisi et renvoie une **décision structurée**
  (validée par un schéma Pydantic), avec les arguments pour/contre et les conditions d'invalidation.
- L'application calcule la **taille de position** à partir de ton capital et du % de risque par trade.

## Installation

Python 3.10 ou plus récent.

```bash
git clone <ce dépôt> && cd teamtrader
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

Crée une clé API sur <https://platform.claude.com/> puis :

```bash
export ANTHROPIC_API_KEY="sk-ant-..."     # Windows PowerShell : $env:ANTHROPIC_API_KEY="sk-ant-..."
```

(Tu peux aussi coller la clé directement dans la barre latérale de l'application.)

## Utilisation

### Interface web

```bash
streamlit run app.py
```

Choisis l'actif, ton style (intraday / swing / position), ton capital et ton risque par trade,
puis clique sur **Lancer l'analyse**. Une analyse prend en général 1 à 3 minutes
(les agents fondamental et géopolitique font des recherches web).

### Ligne de commande

```bash
python -m teamtrader XAUUSD --style swing
python -m teamtrader BTCUSD --style intraday -v     # -v affiche les rapports complets
```

## Réglages (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `TEAMTRADER_MODEL` | `claude-opus-5-5` | Modèle Claude utilisé par tous les agents |
| `TEAMTRADER_SPECIALIST_EFFORT` | `medium` | Profondeur de réflexion des 3 analystes (`low` … `max`) |
| `TEAMTRADER_DECISION_EFFORT` | `high` | Profondeur de réflexion du chef de desk |
| `TEAMTRADER_WEB_SEARCH_MAX_USES` | `6` | Recherches web maximum par agent |

Le coût dépend surtout du nombre de recherches web et de l'effort : baisser l'effort des
spécialistes à `low` ou réduire `TEAMTRADER_WEB_SEARCH_MAX_USES` rend chaque analyse moins chère.

## Structure du code

| Fichier | Contenu |
|---|---|
| `teamtrader/config.py` | Actifs, indicateurs macro, styles de trading, modèle |
| `teamtrader/data.py` | Téléchargement des bougies et du tableau inter-marchés |
| `teamtrader/indicators.py` | EMA, RSI, MACD, Bollinger, ATR, ADX, supports / résistances |
| `teamtrader/agents.py` | Prompts des agents, appels à Claude, schéma de la décision |
| `teamtrader/pipeline.py` | Orchestration, taille de position, risque / rendement |
| `app.py` | Interface Streamlit |
| `tests/` | Tests hors-ligne (données synthétiques, client Claude simulé) |

Ajouter un agent (par exemple « sentiment / positionnement COT ») : créer un `AgentSpec` dans
`agents.py`, l'ajouter à `SPECIALISTS`, ajouter sa clé dans `AgentView.agent`, et lui
préparer un briefing dans `Context.briefing`.

Lancer les tests : `pytest -q`

## Limites à connaître

- **Prix de l'or** : Yahoo ne fournit pas de spot XAU/USD fiable, l'application utilise le
  contrat à terme COMEX `GC=F`, qui cote quelques dollars au-dessus du spot. Vérifie les
  niveaux sur ta plateforme avant de passer un ordre.
- Les données Yahoo peuvent avoir quelques minutes de retard.
- Les agents peuvent se tromper ou s'appuyer sur une information incomplète : lis les
  rapports détaillés et les sources avant d'agir.

> ⚠️ **Outil d'aide à la décision uniquement, pas un conseil en investissement.**
> Le trading comporte un risque de perte en capital.
