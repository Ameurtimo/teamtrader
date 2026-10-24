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
          ✅ Contrôles automatiques (niveaux cohérents, R/R ≥ 1,5, qualité des données)
                                  ▼
          📐 Taille de position avec coûts, marge, levier et limites du compte
                                  ▼
          💼 Paper trading  ·  📒 Historique rejoué et figé → performance mesurée
```

- Chaque spécialiste rédige un rapport et termine par un **signal** (haussier / baissier / neutre + confiance).
- Le chef de desk pondère les avis selon l'horizon choisi et renvoie une **décision structurée**
  (validée par un schéma Pydantic), avec les arguments pour/contre et les conditions d'invalidation.
- La décision passe des **contrôles déterministes** avant d'être affichée ou enregistrée (voir
  plus bas) ; une décision invalide est renvoyée une fois au chef de desk, puis rejetée.
- La **taille de position** tient compte du profil de ton broker (taille du contrat, lots,
  spread, commission, glissement, financement, marge) et des limites du compte (levier, risque
  cumulé, perte journalière maximale).
- **💼 Paper trading** : un portefeuille fictif structuré, avec exécutions simulées et coûts.
- **🎯 Mode Smart Money Concepts** (option) : structure BOS / CHoCH, order blocks, fair value
  gaps, liquidité, premium / discount et **inducement**. Règle stricte : **une entrée n'est
  validée qu'après le balayage de l'inducement** (voir plus bas).
- **📅 Calendrier US + prévisions** : les publications américaines de la semaine (CPI, NFP,
  Fed…) avec, pour chacune, une prévision IA **avant la sortie du chiffre** (au-dessus / en
  dessous / conforme au consensus) et la réaction attendue de l'or, du bitcoin et du dollar.
- L'onglet **Historique et performance** rejoue chaque décision de façon reproductible et
  affiche la courbe de résultats, le drawdown, le profit factor, les résultats par actif, style
  et mode, la **calibration de la confiance** et la **justesse de chaque agent**. Chaque
  analyse peut être rouverte (rapports, sources, paramètres, modèle, qualité des données).
- **💰 Coûts IA** : coût estimé de chaque analyse, dépense du mois et budget maximal.

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
python -m teamtrader BTCUSD --paper                 # ouvre aussi l'ordre en paper trading
```

Les décisions prises en ligne de commande sont aussi ajoutées à l'historique.

## ✅ Contrôles des décisions

Les règles sont écrites dans le prompt du chef de desk, **et imposées par le code**
(`teamtrader/validation.py`) :

- confiance et poids des agents compris entre 0 et 100 ;
- prix positifs et finis ;
- pour un ACHAT ou une VENTE : entrée, stop et au moins un objectif obligatoires ;
- achat : stop < entrée < objectifs ; vente : l'inverse ; objectifs ordonnés ;
- entrée à moins de 10 % du prix actuel ;
- ratio risque / rendement **signé** sur l'objectif 1 d'au moins 1,5 (un objectif du mauvais
  côté donne un ratio négatif, donc refusé).

Une décision qui échoue est renvoyée **une fois** au chef de desk avec la liste des erreurs.
Si elle échoue encore, elle devient **ATTENDRE sans niveaux** (« rejetée »). Le statut des
contrôles (valide, corrigée, rejetée, bloquée) est affiché et enregistré.

**Seuil de qualité** (`teamtrader/quality.py`) : chaque donnée a une date, une source et un
statut (ok, périmée, indisponible). Si le prix est périmé ou absent, si le rapport technique
manque, ou s'il y a moins de 3 rapports exploitables sur 4 (ou pas de rapport SMC en mode
SMC), la décision est **ATTENDRE** sans même appeler le chef de desk.

Sources structurées ajoutées : funding et open interest du perpétuel BTC (OKX, sinon
Binance), position nette des spéculateurs sur l'or (rapport COT de la CFTC), en plus du
Fear & Greed et du calendrier. Les flux ETF restent recherchés sur le web (pas de source
gratuite fiable).

## 💼 Gestion du risque et paper trading

Onglet **Paper trading → Compte, limites de risque et profil broker** :

| Réglage | Défaut | Rôle |
|---|---|---|
| Capital de départ | 10 000 $ | Fonds propres du compte fictif |
| Risque par trade | 1 % | Perte maximale si le stop est touché, **coûts compris** |
| Levier max | ×10 | Nominal total des positions / fonds propres |
| Risque cumulé max | 3 % | Somme des risques des positions ouvertes |
| Perte journalière max | 3 % | Au-delà : aucune nouvelle prise de risque ce jour-là |

Profil broker par actif : taille du contrat (or : 100 oz / lot, BTC : 1 BTC / lot), lot minimum
et pas, spread, glissement, commission (par lot ou en %), financement long / court, marge.
**Les valeurs par défaut sont des ordres de grandeur : remplace-les par celles de ton broker.**

Depuis une décision, le bouton **Ouvrir en paper trading** crée un ordre fictif (entrée, stop,
objectif 1, échéance du style). Il est exécuté sur les vraies bougies qui suivent, avec le même
moteur que l'historique, puis valorisé net de spread, commission, glissement (sorties au stop ou
au marché) et financement. Les positions ouvertes sont transmises aux agents et au chef de desk.

## 💰 Coûts IA

Chaque appel enregistre ses tokens, ses recherches web et un coût estimé (tableau de prix dans
`teamtrader/costs.py`, à vérifier sur la page de tarifs d'Anthropic). Le coût de chaque analyse
est affiché ; l'onglet Historique résume la dépense par mois et par type d'appel.
`TEAMTRADER_MONTHLY_BUDGET_USD` fixe un budget mensuel : une fois atteint, les analyses sont
bloquées.

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
**télécharger une sauvegarde de la base** régulièrement (onglet Historique → Sauvegarde et
restauration), et à la restaurer après un redémarrage.
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
| `TEAMTRADER_MIN_RR` | `1.5` | Ratio risque / rendement minimal sur l'objectif 1 |
| `TEAMTRADER_MAX_ENTRY_DISTANCE_PCT` | `10` | Distance maximale entre l'entrée et le prix actuel |
| `TEAMTRADER_MONTHLY_BUDGET_USD` | `0` (illimité) | Budget IA mensuel ; au-delà, analyses bloquées |
| `TEAMTRADER_LOG_LEVEL` | `INFO` | Niveau des journaux techniques |
| `TEAMTRADER_LOG_FILE` | — | Fichier des journaux (sinon la console) |

Le coût dépend surtout du nombre de recherches web et de l'effort : baisser l'effort des
spécialistes à `low` ou réduire `TEAMTRADER_WEB_SEARCH_MAX_USES` rend chaque analyse moins chère.

## Comment la fiabilité est mesurée (méthode v2)

Le moteur de rejeu (`teamtrader/replay.py`) est commun à l'historique et au paper trading.

- **Données** : bougies 15 min quand elles couvrent la période (sinon 1 h), de la **même source**
  qu'au moment de la décision, avec le **même écart future/spot** pour l'or (enregistré avec la
  décision) : un changement d'écart ultérieur ne modifie pas le résultat.
- **Bougie de la décision** : prise en compte ; ce qui s'y passe est signalé comme ambigu
  (une partie de son range peut précéder la décision).
- **Bougies encore ouvertes** : ignorées.
- **Couverture** : une période n'est terminée que si les données la couvrent vraiment, du début
  (première bougie au plus tard à la décision, sauf marché fermé) jusqu'à l'échéance. Les trous
  inattendus sont signalés (pas le week-end de l'or).
- **Entrée** : immédiate si elle est au prix du marché, sinon quand une bougie touche le prix.
- **Ambiguïtés, hypothèse prudente, toujours signalées** : stop et objectif dans la même bougie =
  perte ; entrée et stop dans la même bougie = perte ; objectif touché dans la bougie d'entrée =
  non compté.
- **Résultats figés** : dès que la période est couverte, le résultat est enregistré avec les
  bougies utilisées et la version de la méthode (🔒 dans le journal). Si la méthode change, le
  résultat est recalculé à partir des bougies conservées, sans dépendre du fournisseur.
- **Statistiques séparées** : objectifs atteints, stops touchés, échéances gagnantes et
  perdantes, « objectif avant stop » et « trades gagnants » (échéances comprises), R moyen et
  cumulé, profit factor, drawdown en R, ventilation par actif / style / mode, calibration de la
  confiance (la confiance de 80 % gagne-t-elle plus souvent que celle de 50 %?).
- **Justesse des agents** : sur les décisions dont l'horizon est écoulé, le signal
  haussier / baissier allait-il dans le sens du marché ? Les signaux neutres ne comptent pas.

Il faut au moins 30 à 50 décisions pour que ces statistiques veuillent dire quelque chose.

## Structure du code

| Fichier | Contenu |
|---|---|
| `teamtrader/config.py` | Actifs, indicateurs macro, styles de trading, modèle |
| `teamtrader/data.py` | Prix (Twelve Data / Yahoo recalé sur le spot), macro, sentiment |
| `teamtrader/indicators.py` | EMA, RSI, MACD, Bollinger, ATR, ADX, supports / résistances |
| `teamtrader/agents.py` | Prompts des agents, appels à Claude, schéma de la décision |
| `teamtrader/pipeline.py` | Orchestration, contrôles, seuil de qualité, archivage d'une analyse |
| `teamtrader/validation.py` | Contrôles déterministes des décisions, R/R signé |
| `teamtrader/quality.py` | Fraîcheur et disponibilité des données, seuil de qualité |
| `teamtrader/replay.py` | Moteur de rejeu (couverture, ambiguïtés, bougies ouvertes) |
| `teamtrader/history.py` | Journal SQLite, résultats figés, performance et calibration |
| `teamtrader/risk.py` | Profil broker, coûts, taille de position et limites du compte |
| `teamtrader/paper.py` | Paper trading : positions fictives, exécutions et coûts |
| `teamtrader/costs.py` | Consommation et coût des appels à Claude, budget mensuel |
| `teamtrader/backup.py` | Sauvegarde / restauration de la base SQLite |
| `teamtrader/logs.py` | Journaux techniques (durées, erreurs) |
| `teamtrader/smc.py` | Détection Smart Money Concepts et setup avec inducement |
| `teamtrader/economic_calendar.py` | Calendrier économique américain de la semaine |
| `teamtrader/calendar_history.py` | Suivi et fiabilité des prévisions du calendrier |
| `app.py` | Interface Streamlit |
| `tests/` | Tests hors ligne (données synthétiques, client Claude simulé, réseau bloqué) |
| `tests/integration/` | Vérifications des vrais fournisseurs et de l'API Claude |
| `tests/app_harness.py` | Interface lancée avec données et modèle simulés (`streamlit run tests/app_harness.py`) |

Ajouter un agent : créer un `AgentSpec` dans `agents.py`, l'ajouter à `SPECIALISTS`, ajouter
sa clé dans `AgentView.agent`, et lui préparer un briefing dans `Context.briefing`.

### Tests

```bash
pytest -q                     # tests unitaires, hors ligne (réseau bloqué volontairement)
pytest -q -m integration      # vrais fournisseurs : Yahoo, gold-api, Forex Factory, OKX/Binance, CFTC…
TEAMTRADER_TEST_CLAUDE=1 pytest -q -m integration   # + 2 petits appels à Claude (quelques centimes)
```

Les tests unitaires tournent automatiquement sur GitHub à chaque push et pull request
(`.github/workflows/tests.yml`). Les tests d'intégration se lancent à la main depuis l'onglet
**Actions → Tests → Run workflow** (case « integration »). Lance-les une première fois : ils
confirment que les formats des API externes sont bien ceux attendus.

## Limites à connaître

- Les données Yahoo peuvent avoir quelques minutes de retard ; le recalage du future de
  l'or sur le spot utilise l'écart du moment de la décision, appliqué à ses bougies.
- Le rejeu ne voit pas l'ordre des mouvements à l'intérieur d'une bougie (15 min au mieux) :
  ces cas sont signalés et tranchés de façon prudente.
- Les coûts du paper trading viennent du profil broker que tu renseignes ; les coûts IA sont
  des estimations.
- La confiance annoncée par le chef de desk reste une appréciation du modèle : regarde la
  table de calibration avant de t'y fier.
- Les agents peuvent se tromper ou s'appuyer sur une information incomplète : lis les
  rapports détaillés et les sources avant d'agir.

> ⚠️ **Outil d'aide à la décision uniquement, pas un conseil en investissement.**
> Le trading comporte un risque de perte en capital.
