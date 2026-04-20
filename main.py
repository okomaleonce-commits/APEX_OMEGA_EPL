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
    from storage.signals_repo import save_signal, save_no_bet, log_pipeline_run

    fixtures = fetch_upcoming(days_ahead=2)
    logger.info(f"{len(fixtures)} matchs a analyser")

    if not fixtures:
        await send_telegram("APEX-EPL -- Aucun match EPL trouve.")
        return

    total_signals = 0

    for fix in fixtures:
        home_name  = fix["home_team"]
        away_name  = fix["away_team"]
        kickoff    = fix["kickoff_utc"]
        fixture_id = fix["fixture_id"]

        logger.info(f"Analyse: {home_name} vs {away_name}")

        home_stats = fetch_team_stats(fix["home_team_id"])
        away_stats = fetch_team_stats(fix["away_team_id"])

        # Blessures filtrees par fixture pour eviter les 100+ retours
        home_inj = fetch_injuries(fix["home_team_id"], fixture_id)
        away_inj = fetch_injuries(fix["away_team_id"], fixture_id)

        h2h       = fetch_h2h(fix["home_team_id"], fix["away_team_id"])
        odds_data = get_odds_for_match(home_name, away_name)

        odds_1x2  = odds_data.get("odds_1x2",  {})
        odds_ou25 = odds_data.get("odds_ou25", {})

        if not odds_1x2:
            odds_1x2, odds_ou25 = get_reference_odds(
                home_name, away_name, home_stats, away_stats
            )

        n_inj_home = len(home_inj)
        n_inj_away = len(away_inj)

        # AIS-F : -5% par blesse cle (max -25%)
        ais_home = max(-0.25, -0.05 * min(n_inj_home, 5))
        ais_away = max(-0.25, -0.05 * min(n_inj_away, 5))

        # DCS
        dcs = 55
        if home_stats.get("matches_played", 0) > 0: dcs += 10
        if away_stats.get("matches_played", 0) > 0: dcs += 10
        if odds_1x2.get("source") == "api_football": dcs += 15
        else:                                         dcs += 5
        if h2h:                                       dcs += 5
        if n_inj_home > 0 or n_inj_away > 0:         dcs += 5

        xg_home, xg_away = compute_xg(
            home_stats, away_stats,
            home_capacity=fix.get("venue_capacity", 0),
            ais_f_home=ais_home,
            ais_f_away=ais_away,
        )
        model    = run_simulation(xg_home, xg_away)
        verdicts = generate_verdicts(model, odds_1x2, odds_ou25, dcs)

        # Sauvegarder dans SQLite
        if verdicts:
            for v in verdicts:
                save_signal({
                    "fixture_id":    fixture_id,
                    "kickoff_utc":   kickoff,
                    "home_team":     home_name,
                    "away_team":     away_name,
                    "market":        v["market"],
                    "outcome":       v["label"],
                    "model_prob":    v["model_prob"],
                    "demargin_prob": v["demargin_prob"],
                    "raw_odds":      v["raw_odds"],
                    "edge":          v["edge"],
                    "max_stake_pct": v["max_stake_pct"],
                    "status":        v["status"],
                    "dcs_score":     dcs,
                    "xg_home":       xg_home,
                    "xg_away":       xg_away,
                    "odds_source":   odds_1x2.get("source", "reference"),
                })
            total_signals += len(verdicts)
        else:
            save_no_bet(fixture_id, home_name, away_name,
                        kickoff, dcs, xg_home, xg_away)

        # Message Telegram
        msg = format_verdict_telegram(
            home_name, away_name, kickoff, model, verdicts, dcs,
            n_inj_home=n_inj_home,
            n_inj_away=n_inj_away,
            odds_source=odds_1x2.get("source", "reference"),
        )
        await send_telegram(msg)

    log_pipeline_run(len(fixtures), total_signals)
    logger.info(f"=== PIPELINE TERMINE === {total_signals} signaux sauvegardes")


async def run_resolver():
    logger.info("=== RESOLUTION SIGNAUX ===")
    from storage.result_resolver import resolve_pending
    stats = await resolve_pending()
    if stats and stats.get("resolved", 0) > 0:
        msg = (
            "=== APEX-EPL BILAN ===\n"
            f"Signaux resolus: {stats['resolved']}\n"
            f"Victoires: {stats['wins']} | Defaites: {stats['losses']}\n"
            f"Win rate: {stats['win_rate']:.1%}\n"
            f"P&L cumule: {stats['pnl_pct']:+.1%}"
        )
        await send_telegram(msg)


async def send_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        for i in range(0, len(text), 4000):
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=text[i:i+4000],
            )
    except Exception as e:
        logger.error(f"Telegram erreur: {e}")


async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    from storage.signals_repo import init_db
    init_db()

    await send_telegram(
        "APEX-OMEGA-EPL demarre\n"
        f"Date: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "SQLite | Dixon-Coles | Labels corriges"
    )

    await run_pipeline()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_pipeline, "cron", hour="8,14,20", minute=0)
    scheduler.add_job(run_resolver, "cron", hour=23, minute=0)
    scheduler.start()
    logger.info("Scheduler: pipeline 08h/14h/20h | resolver 23h")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
