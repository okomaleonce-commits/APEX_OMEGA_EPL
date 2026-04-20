import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
DATA_DIR   = Path(os.getenv("RENDER_DISK_PATH", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Pipeline principal ────────────────────────────────────────────

async def run_pipeline():
    """Fetch fixtures + cotes et publie un résumé sur Telegram."""
    logger.info("=== PIPELINE DÉMARRÉ ===")

    from ingestion.fixtures_service import fetch_upcoming, fetch_team_stats, fetch_injuries
    from ingestion.odds_service import get_odds_for_match

    fixtures = fetch_upcoming(days_ahead=2)
    logger.info(f"{len(fixtures)} matchs à analyser")

    if not fixtures:
        await send_telegram("⚠️ *APEX-EPL* — Aucun match EPL à venir trouvé.")
        return

    lines = [f"📋 *APEX-OMEGA-EPL · {datetime.now(timezone.utc).strftime('%d/%m %H:%M')} UTC*\n"]

    for fix in fixtures:
        home = fix["home_team"]
        away = fix["away_team"]
        ko   = fix["kickoff_utc"][:16].replace("T", " ")

        # Stats
        home_stats = fetch_team_stats(fix["home_team_id"])
        away_stats = fetch_team_stats(fix["away_team_id"])

        # Blessés
        home_inj = fetch_injuries(fix["home_team_id"])
        away_inj = fetch_injuries(fix["away_team_id"])

        # Cotes
        odds = get_odds_for_match(home, away)
        odds_1x2 = odds.get("odds_1x2", {})

        # Résumé
        gf_home = home_stats.get("goals_scored_avg",   "?")
        ga_home = home_stats.get("goals_conceded_avg",  "?")
        gf_away = away_stats.get("goals_scored_avg",   "?")
        ga_away = away_stats.get("goals_conceded_avg",  "?")

        home_raw = odds_1x2.get("home_raw", "—")
        draw_raw = odds_1x2.get("draw_raw", "—")
        away_raw = odds_1x2.get("away_raw", "—")

        n_inj_home = len(home_inj)
        n_inj_away = len(away_inj)

        lines.append(
            f"⚽ *{home}* vs *{away}*\n"
            f"🕐 {ko} UTC\n"
            f"📊 Buts/match: {home} {gf_home}/{ga_home} | {away} {gf_away}/{ga_away}\n"
            f"💰 Cotes: {home_raw} / {draw_raw} / {away_raw}\n"
            f"🏥 Blessés: {home} {n_inj_home} | {away} {n_inj_away}\n"
        )

    await send_telegram("\n".join(lines))
    logger.info("=== PIPELINE TERMINÉ ===")


async def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.warning("Telegram non configuré")
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        # Telegram limite à 4096 caractères par message
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text[i:i+4000],
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Telegram erreur: {e}")


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    await send_telegram(
        "✅ *APEX-OMEGA-EPL démarré*\n"
        f"📅 {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "_Pipeline Étape 1 — Ingestion données active_"
    )

    # Lancer le pipeline immédiatement au démarrage
    await run_pipeline()

    # Scheduler : pipeline toutes les 6 heures
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline,
        trigger="cron",
        hour="8,14,20",
        minute=0,
        id="pipeline_epl"
    )
    scheduler.start()
    logger.info("Scheduler démarré — pipeline à 08h, 14h, 20h UTC")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
