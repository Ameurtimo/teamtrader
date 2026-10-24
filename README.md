# 📈 TeamTrader

Application d'aide à la décision de trading sur **l'or (XAU/USD)** et **le bitcoin (BTC/USD)**,
construite comme une équipe d'agents IA (Claude) :

```
                    ┌───────────────────────────┐
                    │  Données de marché        │  prix spot (Twelve Data ou Yahoo recalé),
                    │  + indicateurs calculés   │  DXY, taux US, VIX, Nasdaq, Fear & Greed…
                    └─────────────┬─────────────┘
      ┌──────────────────┬────────┴─────────┬──────────────────┐   (en parallèle)
      ▼                  ▼                  ▼                  ▼
 📊 Fondamental     📈 Technique      🌍 Géopolitique     🧭 Sentiment
 macro, Fed,        EMA, RSI, MACD,   conflits,           COT, funding,
 flux ETF,          ATR, supports…    sanctions,          Fear & Greed,
 on-chain           (multi-UT)        élections           long/short
 (+ web)                              (+ web)             (+ web)
      └──────────────────┴────────┬─────────┴──────────────────┘
                                  ▼
                   🧠 Chef de desk (agent général)
         lit les 4 rapports → ACHAT / VENTE / ATTENDRE
         entrée, stop-loss, objectifs, confiance, invalidation
                                  ▼
                   📒 Historique : chaque décision est rejouée
                   sur les prix qui ont suivi → fiabilité mesurée
```

- Chaque spécialiste rédige un rapport et termine par un **signal** (haussier / baissier / neutre + confiance).
- Le chef de desk pondère les avis selon l'horizon choisi et renvoie une **décision structurée**
  (validée par un schéma Pydantic), avec les arguments pour/contre et les conditions d'invalidation.
- L'application calcule la **taille de position** à partir de ton capital et du % de risque par trade.
- L'onglet **Historique et fiabilité** enregistre chaque décision, vérifie ensuite si l'objectif
  ou le stop a été touché en premier, et calcule le **taux de réussite**, le **gain moyen en R**
  et le **taux de justesse de chaque agent**. Au bout de quelques dizaines de décisions, tu
  sauras quels agents écouter.

## Installation

Python 3.10 ou plus récent.

```bash
git clone https://github.com/Ameurtimo/teamtrader && cd teamtrader
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

Crée une clé API sur <https://platform.claude.com/> puis :

```bash
export ANTHROPIC_API_KEY="sk-ant-..."     # Windows PowerShell : $env:ANTHROPIC_API_KEY="sk-ant-..."
```

(Tu peux aussi coller la clé directement dans la barre latérale de l'application.)

### Prix spot (recommandé pour l'or)

Les brokers cotent le **spot** XAU/USD. Deux possibilités :

1. **Avec une clé Twelve Data** (gratuite sur <https://twelvedata.com>) :
   `export TWELVEDATA_API_KEY="..."` → bougies spot XAU/USD et BTC/USD directement.
2. **Sans clé** : l'application prend le future COMEX `GC=F` sur Yahoo et le recale sur le
   spot du moment (prix fourni par gold-api.com). Si le spot est indisponible, les prix
   restent ceux du future et l'interface l'indique.

La source utilisée est toujours affichée sous le graphique.

## Utilisation

### Interface web

```bash
streamlit run app.py
```

Choisis l'actif, ton style (intraday / swing / position), ton capital et ton risque par trade,
puis clique sur **Lancer l'analyse**. Une analyse prend en général 1 à 3 minutes
(trois agents font des recherches web).

### Ligne de commande

```bash
python -m teamtrader XAUUSD --style swing
python -m teamtrader BTCUSD --style intraday -v     # -v affiche les rapports complets
```

Les décisions prises en ligne de commande sont aussi ajoutées à l'historique.

## Mettre l'application en ligne

### Vercel : non adapté

Streamlit a besoin d'un serveur qui tourne en continu et garde une connexion WebSocket
ouverte avec chaque visiteur. Vercel exécute des fonctions à la demande, avec une durée
maximale par exécution et un disque effacé entre deux exécutions : l'interface se
déconnecterait et l'historique serait perdu. Il faudrait réécrire l'interface (par exemple
en Next.js) et stocker l'historique dans une vraie base de données.

### Streamlit Community Cloud (gratuit, recommandé)

1. Va sur <https://share.streamlit.io>, connecte ton compte GitHub.
2. **Create app** → dépôt `Ameurtimo/teamtrader`, branche de ton choix, fichier `app.py`.
3. Dans **Advanced settings → Secrets**, colle le contenu de `.streamlit/secrets.toml.example`
   en remplissant tes valeurs. **Définis `TEAMTRADER_PASSWORD`** : sinon n'importe qui ayant
   le lien peut lancer des analyses facturées sur ta clé API.
4. **Deploy**. L'adresse ressemble à `https://teamtrader-xxx.streamlit.app`.

⚠️ Sur Streamlit Cloud, le disque est réinitialisé à chaque redémarrage de l'app : pense à
**exporter l'historique en CSV** régulièrement (bouton dans l'onglet Historique).
Render, Railway ou Fly.io conviennent aussi (commande de démarrage :
`streamlit run app.py --server.port $PORT --server.address 0.0.0.0`) ; ils proposent un disque
persistant pour conserver `teamtrader_history.db`.

## Réglages (variables d'environnement ou secrets)

| Variable | Défaut | Rôle |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Clé API Claude (obligatoire) |
| `TEAMTRADER_PASSWORD` | — | Mot de passe d'accès à l'interface web |
| `TWELVEDATA_API_KEY` | — | Prix spot XAU/USD et BTC/USD via Twelve Data |
| `TEAMTRADER_MODEL` | `claude-opus-5-5` | Modèle Claude utilisé par tous les agents |
| `TEAMTRADER_SPECIALIST_EFFORT` | `medium` | Profondeur de réflexion des analystes (`low` … `max`) |
| `TEAMTRADER_DECISION_EFFORT` | `high` | Profondeur de réflexion du chef de desk |
| `TEAMTRADER_WEB_SEARCH_MAX_USES` | `6` | Recherches web maximum par agent |
| `TEAMTRADER_DB` | `teamtrader_history.db` | Fichier SQLite de l'historique |

Le coût dépend surtout du nombre de recherches web et de l'effort : baisser l'effort des
spécialistes à `low` ou réduire `TEAMTRADER_WEB_SEARCH_MAX_USES` rend chaque analyse moins chère.

## Comment la fiabilité est mesurée

- L'ordre est considéré **déclenché** quand une bougie 1h touche le prix d'entrée
  (immédiatement si l'entrée est au prix du marché). Sans déclenchement avant la fin de
  l'horizon : « non déclenché ».
- Ensuite, on regarde si l'**objectif 1** ou le **stop** est touché en premier. Si une même
  bougie touche les deux, on compte une **perte** (hypothèse prudente).
- Si aucun des deux n'est touché à la fin de l'horizon (24 h en intraday, 10 jours en swing,
  8 semaines en position), le trade est « expiré » et valorisé au dernier cours.
- Les résultats sont exprimés en **R** : +2R = gain de deux fois le risque pris, −1R = stop touché.
- **Justesse des agents** : sur les décisions dont l'horizon est écoulé, le signal
  haussier/baissier de chaque agent allait-il dans le sens du marché ? Les signaux neutres
  ne sont pas comptés.

Il faut au moins 30 à 50 décisions pour que ces statistiques veuillent dire quelque chose.

## Structure du code

| Fichier | Contenu |
|---|---|
| `teamtrader/config.py` | Actifs, indicateurs macro, styles de trading, modèle |
| `teamtrader/data.py` | Prix (Twelve Data / Yahoo recalé sur le spot), macro, sentiment |
| `teamtrader/indicators.py` | EMA, RSI, MACD, Bollinger, ATR, ADX, supports / résistances |
| `teamtrader/agents.py` | Prompts des agents, appels à Claude, schéma de la décision |
| `teamtrader/pipeline.py` | Orchestration, taille de position, risque / rendement |
| `teamtrader/history.py` | Journal SQLite, rejeu des décisions, statistiques de fiabilité |
| `app.py` | Interface Streamlit |
| `tests/` | Tests hors-ligne (données synthétiques, client Claude simulé) |

Ajouter un agent : créer un `AgentSpec` dans `agents.py`, l'ajouter à `SPECIALISTS`, ajouter
sa clé dans `AgentView.agent`, et lui préparer un briefing dans `Context.briefing`.

Lancer les tests : `pytest -q`

## Limites à connaître

- Les données Yahoo peuvent avoir quelques minutes de retard ; le recalage du future de
  l'or sur le spot utilise l'écart du moment, appliqué à tout l'historique.
- Le rejeu des décisions travaille sur des bougies 1h : il ne voit pas l'ordre exact des
  mouvements à l'intérieur d'une heure, ni le spread ou les frais de ton broker.
- Les agents peuvent se tromper ou s'appuyer sur une information incomplète : lis les
  rapports détaillés et les sources avant d'agir.

> ⚠️ **Outil d'aide à la décision uniquement, pas un conseil en investissement.**
> Le trading comporte un risque de perte en capital.
