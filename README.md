# 📈 TeamTrader

Application d'aide à la décision de trading sur **l'or (XAU/USD)** et **le bitcoin (BTC/USD)**,
construite comme une équipe d'agents IA (Claude) :

```
                    ┌───────────────────────────┐
                    │  Données de marché        │  prix spot (Twelve Data ou Yahoo recalé),
                    │  + indicateurs calculés   │  DXY, taux US, VIX, Nasdaq, Fear & Greed,
                    │  + calendrier US          │  CPI / NFP / Fed de la semaine…
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
      + 🎯 Smart Money (option)   + 📅 prévisions de l'économiste (option)
                                  ▼
                   🧠 Chef de desk (agent général)
         lit les rapports → ACHAT / VENTE / ATTENDRE
         (mode SMC : pas d'entrée sans inducement balayé)
         entrée, stop-loss, objectifs, confiance, invalidation
                                  ▼
                   📒 Historique : chaque décision est rejouée
                   sur les prix qui ont suivi → fiabilité mesurée
```

- Chaque spécialiste rédige un rapport et termine par un **signal** (haussier / baissier / neutre + confiance).
- Le chef de desk pondère les avis selon l'horizon choisi et renvoie une **décision structurée**
  (validée par un schéma Pydantic), avec les arguments pour/contre et les conditions d'invalidation.
- L'application calcule la **taille de position** à partir de ton capital et du % de risque par trade.
- **🎯 Mode Smart Money Concepts** (option) : structure BOS / CHoCH, order blocks, fair value
  gaps, liquidité, premium / discount et **inducement**. Règle stricte : **une entrée n'est
  validée qu'après le balayage de l'inducement** (voir plus bas).
- **📅 Calendrier US + prévisions** : les publications américaines de la semaine (CPI, NFP,
  Fed…) avec, pour chacune, une prévision IA **avant la sortie du chiffre** (au-dessus / en
  dessous / conforme au consensus) et la réaction attendue de l'or, du bitcoin et du dollar.
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
(plusieurs agents font des recherches web).

### Ligne de commande

```bash
python -m teamtrader XAUUSD --style swing
python -m teamtrader BTCUSD --style intraday -v     # -v affiche les rapports complets
python -m teamtrader XAUUSD --style intraday --smc  # mode Smart Money Concepts
```

Les décisions prises en ligne de commande sont aussi ajoutées à l'historique.

## 🎯 Mode Smart Money Concepts

Active **Mode Smart Money Concepts** dans la barre latérale. Un 5ᵉ analyste rejoint l'équipe
et l'onglet **Scanner SMC** affiche la lecture SMC en direct, **instantanément et gratuitement**
(calcul local, aucun appel à l'IA) : pratique pour surveiller si l'inducement a été pris.

Unités de temps utilisées :

| Style | Setup | Biais (UT supérieure) |
|---|---|---|
| Intraday | 15 min | 1 h |
| Swing | 1 h | 4 h |
| Position | 4 h | journalier |

Ce que le logiciel détecte (dans `teamtrader/smc.py`) :

- **Swings** de structure (5 bougies de chaque côté) et swings internes (2 bougies) ;
- **BOS / CHoCH** : cassure, sur clôture, du dernier swing dans le sens / à contre-sens de la tendance ;
- **Order blocks** : dernière bougie opposée avant l'impulsion qui casse la structure ;
- **FVG** : déséquilibres de 3 bougies non comblés ;
- **Liquidité** : égaux hauts / égaux bas non balayés ;
- **Premium / discount** : niveau 50 % de la jambe en cours ;
- **Inducement (IDM)** : dernier repli interne formé entre le BOS et l'extrême de la jambe.
- **POI** : order block (de préférence) ou FVG situé au-delà de l'inducement, en discount pour
  un achat (premium pour une vente). Entrée sur le bord proche du POI, stop au-delà du POI
  (au moins 0,5 ATR), objectif 1 = extrême de la jambe, objectif 2 = liquidité suivante.

**La règle d'inducement est appliquée par le code, pas seulement demandée à l'IA** :

| Situation | Résultat |
|---|---|
| Inducement pas encore formé ou pas encore balayé | Décision forcée à **ATTENDRE** ; le plan est gardé comme ordre conditionnel, avec le niveau d'IDM à surveiller |
| Inducement balayé, décision dans le sens du setup | Entrée **validée** (ordre limite sur le POI) |
| Inducement balayé mais décision contre le setup | Forcée à **ATTENDRE** |
| Origine de la jambe ou order block cassé | Setup **invalidé** |

L'historique distingue les décisions **standard** et **SMC** : tu peux comparer leur fiabilité.

## 📅 Calendrier économique US et prévisions

L'onglet **Calendrier US** liste les publications américaines de la semaine à impact fort et
moyen (source : flux public Forex Factory), avec le consensus et le chiffre précédent, aux
heures de Paris (`TEAMTRADER_TZ` pour changer de fuseau).

Le bouton **Prédire la direction des publications** lance un économiste IA qui, pour chaque
publication importante et **avant sa sortie** :

1. cherche les indicateurs avancés récents (ADP, inscriptions au chômage et ISM avant le NFP ;
   nowcast de la Fed de Cleveland avant le CPI ; GDPNow avant le PIB ; FedWatch avant la Fed…) ;
2. prévoit si le chiffre sortira **au-dessus, en dessous ou conforme** au consensus, avec une
   estimation et une probabilité ;
3. indique la réaction attendue du **dollar, de l'or et du bitcoin**.

Ces prévisions sont ensuite transmises au chef de desk lors des analyses, et le calendrier
de la semaine est toujours fourni à l'analyste fondamental et au chef de desk (qui évite
d'entrer juste avant une annonce majeure).

### Fiabilité du prédicteur

Chaque prévision est enregistrée automatiquement. Une fois la publication passée, le chiffre
publié est récupéré :

1. depuis le flux du calendrier quand il le fournit ;
2. sinon avec le bouton **Récupérer les chiffres publiés manquants (IA)** (recherche web) ;
3. ou en le saisissant toi-même dans la colonne **Chiffre publié** du tableau.

L'application compare alors le chiffre au consensus (à la précision du consensus : 0,3 % contre
0,3 % = conforme) et affiche :

- le **taux de prévisions justes** (un tirage au hasard ferait environ 33 %) ;
- le taux quand l'économiste annonçait une **surprise** (hors « conforme ») ;
- la **calibration** : quand il annonce 65 %, a-t-il raison 65 % du temps ?
- la justesse de la **réaction annoncée de l'or et du BTC** dans l'heure suivant la publication
  (variation de moins de 0,10 % pour l'or et 0,25 % pour le BTC = neutre).

Si une publication a été prévue plusieurs fois, seule la dernière prévision faite **avant**
sa sortie compte. À partir de 5 prévisions vérifiées, ce bilan est transmis à l'économiste
pour qu'il ajuste ses probabilités.

⚠️ Battre le consensus est difficile : considère ces prévisions comme un éclairage, pas comme
une certitude. L'économiste a pour consigne de ne pas dépasser 65-70 % de probabilité sans
indices très nets.

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
| `TEAMTRADER_TZ` | `Europe/Paris` | Fuseau horaire d'affichage du calendrier |

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
| `teamtrader/smc.py` | Détection Smart Money Concepts et setup avec inducement |
| `teamtrader/economic_calendar.py` | Calendrier économique américain de la semaine |
| `teamtrader/calendar_history.py` | Suivi et fiabilité des prévisions du calendrier |
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
