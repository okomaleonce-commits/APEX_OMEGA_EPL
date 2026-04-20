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


async def run_pipeline():
    logger.info("=== PIPELINE DEMARRE ===")

    from ingestion.fixtures_service import (
        fetch_upcoming, fetch_team_stats, fetch_injuries, fetch_h2h
    )
    from ingestion.odds_service import get_odds_for_match, get_reference_odds
    from models.dixon_coles import compute_xg, run_simulation
    from decisions.verdict_engine import generate_verdicts, format_verdict_telegram

    fixtures = fetch_upcoming(days_ahead=2)
    logger.info(f"{len(fixtures)} matchs a analyser")

    if not fixtures:
        await send_telegram("*APEX-EPL* — Aucun match EPL trouve.")
        return

    for fix in fixtures:
        home_name = fix["home_team"]
        away_name = fix["away_team"]
        kickoff   = fix["kickoff_utc"]

        logger.info(f"Analyse: {home_name} vs {away_name}")

        # Donnees
        home_stats = fetch_team_stats(fix["home_team_id"])
        away_stats = fetch_team_stats(fix["away_team_id"])
        home_inj   = fetch_injuries(fix["home_team_id"])
        away_inj   = fetch_injuries(fix["away_team_id"])
        h2h        = fetch_h2h(fix["home_team_id"], fix["away_team_id"])
        odds_data  = get_odds_for_match(home_name, away_name)

        odds_1x2  = odds_data.get("odds_1x2",  {})
        odds_ou25 = odds_data.get("odds_ou25", {})

        # Fallback cotes de reference si API vide
        if not odds_1x2:
            odds_1x2, odds_ou25 = get_reference_odds(
                home_name, away_name, home_stats, away_stats
            )

        # AIS-F : -5% par blesse cle (max -25%)
        ais_home = max(-0.25, -0.05 * min(len(home_inj), 5))
        ais_away = max(-0.25, -0.05 * min(len(away_inj), 5))

        # DCS
        dcs = 55
        if home_stats.get("matches_played", 0) > 0: dcs += 10
        if away_stats.get("matches_played", 0) > 0: dcs += 10
        if odds_1x2.get("source") == "api_football":  dcs += 15
        else:                                          dcs += 5
        if h2h:  dcs += 5
        if home_inj or away_inj: dcs += 5

        # Modele Dixon-Coles
        xg_home, xg_away = compute_xg(
            home_stats, away_stats,
            home_capacity=fix.get("venue_capacity", 0),
            ais_f_home=ais_home,
            ais_f_away=ais_away,
        )
        model = run_simulation(xg_home, xg_away)

        # Verdicts
        verdicts = generate_verdicts(model, odds_1x2, odds_ou25, dcs)

        # Infos supplementaires
        inj_home_str = f"{len(home_inj)} blesse(s)" if home_inj else "aucun"
        inj_away_str = f"{len(away_inj)} blesse(s)" if away_inj else "aucun"
        source_str   = odds_1x2.get("source", "reference")

        msg = format_verdict_telegram(
            home_name, away_name, kickoff, model, verdicts, dcs
        )

        # Ajouter infos contexte
        context = (
            f"\n_Blessés: {home_name} {inj_home_str} | "
            f"{away_name} {inj_away_str}_\n"
            f"_Source cotes: {source_str}_"
        )
        msg = msg + context

        await send_telegram(msg)

    logger.info("=== PIPELINE TERMINE ===")


async def send_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.warning("Telegram non configure")
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text[i:i+4000],
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Telegram erreur: {e}")


async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    await send_telegram(
        "*APEX-OMEGA-EPL demarre*\n"
        f"Date: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "_Dixon-Coles + Fallback cotes actifs_"
    )

    await run_pipeline()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_pipeline, "cron", hour="8,14,20", minute=0)
    scheduler.start()
    logger.info("Scheduler actif — pipeline 08h/14h/20h UTC")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
