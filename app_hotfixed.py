"""
APEX-OMEGA-EPL — Render Entrypoint v3

Start Command Render : python app_hotfixed.py

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN          — Token BotFather (ne jamais logger)
  TELEGRAM_CHANNEL_ID         — ID du canal
  FOOTBALL_DATA_API_KEY       — Clé api-football.com
  RENDER_DISK_PATH            — /var/data
  PYTHON_VERSION              — 3.11.0

Variables optionnelles :
  TELEGRAM_ALLOWED_USERS      — IDs autorisés séparés par virgule (ex: 123456,789012)
  ALLOW_REFERENCE_ODDS_SIGNALS — true (défaut)
  ALLOW_UNCONFIRMED_ACL        — true (défaut)
  ACL_MAX_INJURY_LIST_SIZE     — 5 (défaut)
  FOOTBALL_DATA_ORG_TOKEN      — Token football-data.org (gratuit)
"""

import os
import logging

import runtime_hotfix  # patches: odds params, ACL gate, verdict gate, free data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def _is_set(var: str) -> str:
    """Retourne 'SET' ou 'NOT SET' sans jamais logger la valeur."""
    return "SET" if os.getenv(var) else "NOT SET"


# Log de démarrage — aucune valeur sensible exposée
logger.info("=== APEX-OMEGA-EPL DÉMARRAGE ===")
logger.info(f"TELEGRAM_BOT_TOKEN           : {_is_set('TELEGRAM_BOT_TOKEN')}")
logger.info(f"TELEGRAM_CHANNEL_ID          : {_is_set('TELEGRAM_CHANNEL_ID')}")
logger.info(f"FOOTBALL_DATA_API_KEY        : {_is_set('FOOTBALL_DATA_API_KEY')}")
logger.info(f"TELEGRAM_ALLOWED_USERS       : {_is_set('TELEGRAM_ALLOWED_USERS')}")
logger.info(f"ALLOW_REFERENCE_ODDS_SIGNALS : {os.getenv('ALLOW_REFERENCE_ODDS_SIGNALS', 'true (défaut)')}")
logger.info(f"ALLOW_UNCONFIRMED_ACL        : {os.getenv('ALLOW_UNCONFIRMED_ACL', 'true (défaut)')}")
logger.info(f"ACL_MAX_INJURY_LIST_SIZE     : {os.getenv('ACL_MAX_INJURY_LIST_SIZE', '5 (défaut)')}")
logger.info("================================")

import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main.main())
