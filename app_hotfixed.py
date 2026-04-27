"""
APEX-OMEGA-EPL — Render Entrypoint v2

Start Command Render : python app_hotfixed.py

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN          — Token BotFather
  TELEGRAM_CHANNEL_ID         — ID du canal
  FOOTBALL_DATA_API_KEY       — Clé api-football.com (dashboard.api-football.com)
  RENDER_DISK_PATH            — /var/data
  PYTHON_VERSION              — 3.11.0

Variables optionnelles (defaults permissifs) :
  ALLOW_REFERENCE_ODDS_SIGNALS  — true  (signaux même sans vraies cotes)
  ALLOW_UNCONFIRMED_ACL         — true  (ACL actif même sans compos confirmées)
  ENABLE_FREE_AUTO_DATA         — true  (fallback football-data.org)
  FOOTBALL_DATA_ORG_TOKEN       — Token football-data.org (gratuit)
"""

import os
import logging
import runtime_hotfix  # patches: odds params, ACL gate, verdict gate, free data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Log des gates actifs au démarrage
logger.info("=== APEX-OMEGA-EPL GATES ===")
logger.info(f"ALLOW_REFERENCE_ODDS_SIGNALS : {os.getenv('ALLOW_REFERENCE_ODDS_SIGNALS', 'true (default)')}")
logger.info(f"ALLOW_UNCONFIRMED_ACL        : {os.getenv('ALLOW_UNCONFIRMED_ACL', 'true (default)')}")
logger.info(f"ENABLE_FREE_AUTO_DATA        : {os.getenv('ENABLE_FREE_AUTO_DATA', 'true (default)')}")
logger.info(f"FOOTBALL_DATA_ORG_TOKEN      : {'SET' if os.getenv('FOOTBALL_DATA_ORG_TOKEN') else 'NOT SET'}")
logger.info(f"FOOTBALL_DATA_API_KEY        : {'SET (' + os.getenv('FOOTBALL_DATA_API_KEY','')[:4] + '...)' if os.getenv('FOOTBALL_DATA_API_KEY') else 'NOT SET'}")
logger.info("============================")

import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main.main())
