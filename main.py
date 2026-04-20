import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
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


# ── Helpers ───────────────────────────────────────────────────────

async def send_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=CHANNEL_ID, text=text[i:i+4000])
    except Exception as e:
        logger.error(f"Telegram erreur: {e}")


def build_rule_context(fix, home_inj, away_inj, h2h, home_tier, away_tier):
    h2h_avg = sum(m.get("total_goals", 0) for m in h2h) / len(h2h) if h2h else 0.0
    home_acl = len(home_inj) >= 3
    away_acl = len(away_inj) >= 3
    home_stake = 6 if home_tier <= 2 else (8 if home_tier == 5 else 3)
    away_stake = 6 if away_tier <= 2 else (8 if away_tier == 5 else 3)
    return {
        "home_tier": home_tier, "away_tier": away_tier,
        "venue_capacity": fix.get("venue_capacity", 0),
        "home_fatigue_coeff": 1.0, "away_fatigue_coeff": 1.0,
        "home_inj_count": len(home_inj), "away_inj_count": len(away_inj),
        "home_acl_active": home_acl, "away_acl_active": away_acl,
        "home_acl_factor": 2.0 if len(home_inj) >= 4 else 1.5,
        "away_acl_factor": 2.0 if len(away_inj) >= 4 else 1.5,
        "te_home": False, "te_away": False, "te_goals": 0,
        "eba_away": False, "eba_home": False,
        "h2h_avg_goals": round(h2h_avg, 2),
        "home_ccr_ratio": 1.0, "away_ccr_ratio": 1.0,
        "home_stake_score": home_stake, "away_stake_score": away_stake,
    }


# ── Pipeline principal ────────────────────────────────────────────

async def run_pipeline():
    logger.info("=== PIPELINE DEMARRE ===")

    from ingestion.fixtures_service import (
        fetch_upcoming, fetch_team_stats, fetch_injuries, fetch_h2h
    )
    from ingestion.odds_service import get_odds_for_match, get_reference_odds
    from models.dixon_coles import compute_xg, run_simulation
    from rules.rule_engine import apply_all_rules, get_tier
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
        home_inj   = fetch_injuries(fix["home_team_id"], fixture_id)
        away_inj   = fetch_injuries(fix["away_team_id"], fixture_id)
        h2h        = fetch_h2h(fix["home_team_id"], fix["away_team_id"])
        odds_data  = get_odds_for_match(home_name, away_name)

        odds_1x2  = odds_data.get("odds_1x2",  {})
        odds_ou25 = odds_data.get("odds_ou25", {})

        if not odds_1x2:
            odds_1x2, odds_ou25 = get_reference_odds(
                home_name, away_name, home_stats, away_stats
            )

        n_inj_home = len(home_inj)
        n_inj_away = len(away_inj)

        ais_home = max(-0.25, -0.05 * min(n_inj_home, 5))
        ais_away = max(-0.25, -0.05 * min(n_inj_away, 5))

        dcs = 55
        if home_stats.get("matches_played", 0) > 0: dcs += 10
        if away_stats.get("matches_played", 0) > 0: dcs += 10
        if odds_1x2.get("source") == "api_football": dcs += 15
        else:                                         dcs += 5
        if h2h:                                       dcs += 5
        if n_inj_home > 0 or n_inj_away > 0:         dcs += 5

        home_tier = get_tier(home_name)
        away_tier = get_tier(away_name)

        xg_home, xg_away = compute_xg(
            home_stats, away_stats,
            home_capacity=fix.get("venue_capacity", 0),
            ais_f_home=ais_home,
            ais_f_away=ais_away,
        )
        model = run_simulation(xg_home, xg_away)
        probs = {"home": model["home"], "draw": model["draw"], "away": model["away"]}

        rule_ctx = build_rule_context(
            fix, home_inj, away_inj, h2h, home_tier, away_tier
        )
        adj_probs, moratoriums, rules_active = apply_all_rules(
            home_name, away_name, probs, rule_ctx
        )

        model["home"] = adj_probs.get("home", model["home"])
        model["draw"] = adj_probs.get("draw", model["draw"])
        model["away"] = adj_probs.get("away", model["away"])

        blocked_markets = {m["market"] for m in moratoriums}
        r15 = any("R15" in r for r in rules_active)

        verdicts = generate_verdicts(
            model, odds_1x2, odds_ou25, dcs,
            moratoriums=list(blocked_markets),
            r15_active=r15,
        )

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

        msg = format_verdict_telegram(
            home_name, away_name, kickoff, model, verdicts, dcs,
            n_inj_home=n_inj_home, n_inj_away=n_inj_away,
            odds_source=odds_1x2.get("source", "reference"),
            rules_active=rules_active, moratoriums=moratoriums,
        )
        await send_telegram(msg)

    log_pipeline_run(len(fixtures), total_signals)
    logger.info(f"=== PIPELINE TERMINE === {total_signals} signaux sauvegardes")


async def run_resolver():
    logger.info("=== RESOLUTION SIGNAUX ===")
    from storage.result_resolver import resolve_pending
    stats = await resolve_pending()
    if stats and stats.get("resolved", 0) > 0:
        await send_telegram(
            "=== APEX-EPL BILAN ===\n"
            f"Resolus  : {stats['resolved']}\n"
            f"Victoires: {stats['wins']} | Defaites: {stats['losses']}\n"
            f"Win rate : {stats['win_rate']:.1%}\n"
            f"P&L      : {stats['pnl_pct']:+.1%}"
        )


# ── Commandes Telegram ────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_start
    await handle_start(ctx.bot, update.effective_chat.id)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_status
    await handle_status(ctx.bot, update.effective_chat.id)

async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_analyse
    await handle_analyse(ctx.bot, update.effective_chat.id, run_pipeline)

async def cmd_bilan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_bilan
    await handle_bilan(ctx.bot, update.effective_chat.id)

async def cmd_api(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_api
    await handle_api(ctx.bot, update.effective_chat.id)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_help
    await handle_help(ctx.bot, update.effective_chat.id)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    from storage.signals_repo import init_db
    init_db()

    # Lancer l'analyse initiale (sans bloquer)
    await send_telegram(
        "APEX-OMEGA-EPL v1.3 demarre\n"
        f"Date: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "11 regles v1.3 | SQLite | Commandes actives\n"
        "Tapez /help pour la liste des commandes"
    )
    await run_pipeline()

    # Scheduler taches automatiques
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_pipeline, "cron", hour="8,14,20", minute=0)
    scheduler.add_job(run_resolver, "cron", hour=23, minute=0)
    scheduler.start()
    logger.info("Scheduler: pipeline 08h/14h/20h | resolver 23h")

    # Bot Telegram avec polling pour les commandes
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("bilan",   cmd_bilan))
    app.add_handler(CommandHandler("api",     cmd_api))
    app.add_handler(CommandHandler("help",    cmd_help))

    logger.info("Bot polling demarre — commandes actives")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Boucle infinie
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
