# APEX_OMEGA_EPL

Bot Telegram d'analyse EPL avec moteur Dixon-Coles, règles contextuelles APEX, détection de value betting et enrichissement optionnel par APIs externes.

## Sources de données

Le bot conserve son socle existant API-Football/API-Sports et ajoute deux couches optionnelles :

- **FootyStats** : enrichissement statistique BTTS, Over 2.5, moyenne de buts et cotes disponibles.
- **Odds-API.io** : récupération des meilleures cotes multi-bookmakers pour 1X2, Over/Under 2.5 et BTTS.

Si `FOOTYSTATS_API_KEY` ou `ODDS_API_IO_KEY` ne sont pas configurées, le bot continue de fonctionner avec les sources déjà présentes et le modèle de référence.

## Variables Render à configurer

Variables déjà utilisées :

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=...
FOOTBALL_DATA_API_KEY=...
EPL_LEAGUE_ID=39
EPL_SEASON=2025
RENDER_DISK_PATH=./data
```

Variables ajoutées :

```env
ENABLE_FOOTYSTATS=true
FOOTYSTATS_API_KEY=...
FOOTYSTATS_BASE_URL=https://api.football-data-api.com
FOOTYSTATS_TIMEZONE=UTC
FOOTYSTATS_MAX_PAGES=2

ENABLE_ODDS_API_IO=true
ODDS_API_IO_KEY=...
ODDS_API_IO_BASE_URL=https://api.odds-api.io/v3
ODDS_API_BOOKMAKERS=Bet365,Pinnacle,Unibet
ODDS_API_LEAGUE_SLUG=england-premier-league
```

## Ce que la v1.4 ajoute

- Service `ingestion/footystats_service.py` : appels FootyStats, cache, matching de noms d'équipes, extraction BTTS / Over 2.5 / cotes.
- Service `ingestion/odds_api_io_service.py` : recherche d'événements, extraction des meilleurs prix multi-bookmakers, demarginisation 1X2 / O-U 2.5 / BTTS.
- Service `ingestion/enrichment_service.py` : coordination des enrichissements externes avec fallback sécurisé.
- `main.py` : branchement de l'enrichissement dans le pipeline.
- `decisions/verdict_engine.py` : activation des verdicts BTTS Oui / BTTS Non avec calcul d'edge.

## Logique de prudence

FootyStats ne remplace pas brutalement le modèle interne. Les probabilités externes BTTS et Over 2.5 sont fusionnées prudemment avec le modèle :

- modèle interne : 65 % du poids ;
- donnée FootyStats : 35 % du poids.

Odds-API.io est prioritaire pour les cotes réelles lorsqu'un match est correctement reconnu. Sinon, le bot conserve API-Football ou le modèle de référence.

## Commandes Telegram

```text
/start
/help
/status
/analyse
/bilan
/api
/refresh
```

## Déploiement

Après merge de la PR :

1. Ajouter les variables Render ci-dessus.
2. Relancer un manual deploy.
3. Vérifier les logs :
   - `APEX-OMEGA-EPL v1.4 operationnel`
   - `Enrichissement: footystats` ou `odds_api_io` dans les messages Telegram quand les données sont trouvées.
4. Si une API consomme trop de quota, désactiver temporairement avec :

```env
ENABLE_FOOTYSTATS=false
ENABLE_ODDS_API_IO=false
```
