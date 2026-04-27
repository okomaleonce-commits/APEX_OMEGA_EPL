# APEX-OMEGA-EPL

Bot Telegram d'analyse de paris sportifs pour la Premier League anglaise.
Modèle Dixon-Coles + Monte-Carlo 50k · 11 règles contextuelles v1.3 · Critère de Kelly.

---

## Architecture

```
apex_omega_epl/
├── main.py                    # Orchestrateur + polling Telegram
├── app_hotfixed.py            # Entrypoint Render
├── ingestion/
│   ├── fixtures_service.py    # Fixtures, stats, H2H, blessures (API-Football)
│   ├── odds_service.py        # Cotes + démarginisation
│   └── free_data_service.py   # Fallback football-data.org
├── models/
│   └── dixon_coles.py         # Dixon-Coles + Monte-Carlo 50 000 itérations
├── rules/
│   └── rule_engine.py         # 11 règles contextuelles (ACL, TE, EBA, HSP, CCR, ESY)
├── decisions/
│   └── verdict_engine.py      # Génération signaux + Kelly
├── storage/
│   ├── signals_repo.py        # Persistance SQLite
│   └── result_resolver.py     # Résolution post-match
└── interfaces/
    └── telegram_commands.py   # Commandes /start /help /analyse /status /bilan
```

## Déploiement Render

**Type :** Background Worker  
**Build Command :** `pip install -r requirements.txt`  
**Start Command :** `python app_hotfixed.py`  
**Disk :** `/var/data` · 1 GB (persistant)

## Variables d'environnement

| Variable | Requis | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token BotFather |
| `TELEGRAM_CHANNEL_ID` | ✅ | ID du canal Telegram |
| `FOOTBALL_DATA_API_KEY` | ✅ | Clé api-football.com |
| `RENDER_DISK_PATH` | ✅ | `/var/data` |
| `PYTHON_VERSION` | ✅ | `3.11.0` |
| `TELEGRAM_ALLOWED_USERS` | ✅ | IDs autorisés (virgule) |
| `ALLOW_REFERENCE_ODDS_SIGNALS` | ⚙️ | `true` — signaux sans vraies cotes |
| `ALLOW_UNCONFIRMED_ACL` | ⚙️ | `true` — ACL sans compos confirmées |
| `ACL_MAX_INJURY_LIST_SIZE` | ⚙️ | `5` — cap blessés actuels |
| `FOOTBALL_DATA_ORG_TOKEN` | ⚙️ | Token football-data.org (gratuit) |

## Commandes Telegram

| Commande | Description | Accès |
|---|---|---|
| `/start` | Message d'accueil | Public |
| `/help` | Liste des commandes | Public |
| `/status` | Stats DB + état bot | Admin |
| `/analyse` | Lance le pipeline immédiatement | Admin |
| `/bilan` | P&L par marché | Admin |
| `/api` | Test connexion APIs | Admin |
| `/refresh` | Vider le cache | Admin |

## Pipeline analytique

1. Fetch fixtures EPL (J+7)
2. Stats équipes + blessures + H2H
3. Cotes API-Football → démarginisation Pinnacle
4. Dixon-Coles + Monte-Carlo 50k itérations
5. 11 règles contextuelles (moratoriums, redistributions)
6. Value detection (edge ≥ seuil par marché)
7. Kelly fractionné + caps bankroll
8. Publication Telegram

**Scheduler :** 08h · 14h · 20h UTC (pipeline) · 23h UTC (resolver résultats)

## Sécurité

- Contrôle d'accès via `TELEGRAM_ALLOWED_USERS`
- Aucune valeur sensible dans les logs
- Requêtes SQL paramétrées (protection injection)
- Tokens gérés exclusivement via variables d'environnement Render

## Licence

Usage analytique privé — Leonce Abro Okoma · 2026
