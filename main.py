import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict, NetworkError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "") or os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "") or os.getenv("CHAT_ID", "")
DATA_DIR   = Path(os.getenv("RENDER_DISK_PATH", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────

async def send_channel(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=CHANNEL_ID, text=text[i:i+4000])
    except Exception as e:
        logger.error(f"Telegram canal erreur: {e}")


def get_tier(team_name):
    TIER_1 = {"Arsenal", "Manchester City", "Liverpool"}
    TIER_2 = {"Chelsea", "Newcastle United", "Manchester United", "Tottenham"}
    TIER_3 = {"Aston Villa", "Brighton", "Fulham", "West Ham", "Bournemouth"}
    TIER_4 = {"Brentford", "Crystal Palace", "Everton", "Wolves"}
    if team_name in TIER_1: return 1
    if team_name in TIER_2: return 2
    if team_name in TIER_3: return 3
    if team_name in TIER_4: return 4
    return 5


def _acl_check(injured):
    """Calcule acl_check depuis la liste des blesses."""
    by_line = {}
    for p in injured:
        line = p.get("line", "defense")
        if line in ("defense", "midfield", "attack"):
            by_line[line] = by_line.get(line, 0) + 1
    factor, flag = 1.0, None
    for line, count in by_line.items():
        if count >= 4:
            factor, flag = 2.0, "EFFONDREMENT_LIGNE"
            break
        elif count >= 3:
            factor, flag = max(factor, 1.5), "LIGNE_CRITIQUE"
    return {"active": flag is not None, "factor": factor,
            "flag": flag, "by_line": by_line}


def _lineup_dict(injured):
    """Formate lineup au format dict attendu par r9_acl."""
    players = [{"name": p.get("name", ""), "line": "defense"} for p in injured]
    return {"injured_players": players, "acl_check": _acl_check(players),
            "lineup_confirmed": False, "ais_f_raw": 0.0}


def build_match_ctx(fix, home_inj, away_inj, h2h,
                    home_stats, away_stats, home_tier, away_tier):
    h2h_avg = (sum(m.get("total_goals", 0) for m in h2h) / len(h2h)
               if h2h else 0.0)
    return {
        "home_name":  fix["home_team"],
        "away_name":  fix["away_team"],
        "home_tier":  home_tier,
        "away_tier":  away_tier,
        "home_stake": 8 if home_tier <= 2 else (10 if home_tier == 5 else 3),
        "away_stake": 8 if away_tier <= 2 else (10 if away_tier == 5 else 3),
        "home_lineup":     _lineup_dict(home_inj),
        "away_lineup":     _lineup_dict(away_inj),
        "h2h_matches":     h2h,
        "h2h_avg_goals":   round(h2h_avg, 2),
        "home_te_goals":   0, "home_te_hours": 999,
        "away_te_goals":   0, "away_te_hours": 999,
        "away_eba_active":    False,
        "away_eba_victory":   False,
        "away_eba_rotations": 0,
        "home_raw_odds":   None,
        "away_raw_odds":   None,
        "home_stats":      home_stats,
        "away_stats":      away_stats,
        "home_ccr_ratio":  1.0,
        "away_ccr_ratio":  1.0,
        "venue_capacity":  fix.get("venue_capacity", 0),
    }


# ── Pipeline ──────────────────────────────────────────────────────

async def run_pipeline():
    logger.info("=== PIPELINE DEMARRE ===")
    try:
        from ingestion.fixtures_service import (
            fetch_upcoming, fetch_team_stats, fetch_injuries, fetch_h2h
        )
        from ingestion.odds_service import get_odds_for_match, get_reference_odds
        from models.dixon_coles import compute_xg, run_simulation
        from rules.rule_engine import apply_all_rules
        from decisions.verdict_engine import generate_verdicts, format_verdict_telegram
        from storage.signals_repo import save_signal, save_no_bet, log_pipeline_run

        fixtures = fetch_upcoming(days_ahead=7)
        logger.info(f"{len(fixtures)} matchs a analyser")

        if not fixtures:
            await send_channel("APEX-EPL -- Aucun match EPL trouve.")
            return

        total_signals = 0

        for fix in fixtures:
            home_name  = fix["home_team"]
            away_name  = fix["away_team"]
            kickoff    = fix["kickoff_utc"]
            fixture_id = fix["fixture_id"]
            logger.info(f"Analyse: {home_name} vs {away_name}")

            try:
                home_stats = fetch_team_stats(fix["home_team_id"])
                away_stats = fetch_team_stats(fix["away_team_id"])
                # Securite : s'assurer que les stats sont des dicts
                if not isinstance(home_stats, dict): home_stats = {}
                if not isinstance(away_stats, dict): away_stats = {}
                home_inj   = fetch_injuries(fix["home_team_id"])
                away_inj   = fetch_injuries(fix["away_team_id"])
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
                ais_home   = max(-0.25, -0.05 * min(n_inj_home, 5))
                ais_away   = max(-0.25, -0.05 * min(n_inj_away, 5))
                home_tier  = get_tier(home_name)
                away_tier  = get_tier(away_name)

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
                    ais_f_home=ais_home, ais_f_away=ais_away,
                )
                model = run_simulation(xg_home, xg_away)
                probs = {"home": model["home"], "draw": model["draw"],
                         "away": model["away"]}

                match_ctx = build_match_ctx(
                    fix, home_inj, away_inj, h2h,
                    home_stats, away_stats, home_tier, away_tier
                )
                # Injecter les cotes dans le contexte pour R5 Big6 Away
                match_ctx["home_raw_odds"] = odds_1x2.get("home_raw", 1.5)
                match_ctx["away_raw_odds"] = odds_1x2.get("away_raw", 2.5)
                adj_probs, moratoriums, rules_active, xg_home, xg_away, multi_rule = apply_all_rules(
                    match_ctx, probs, xg_home, xg_away
                )
                model["home"] = adj_probs.get("home", model["home"])
                model["draw"] = adj_probs.get("draw", model["draw"])
                model["away"] = adj_probs.get("away", model["away"])

                verdicts = generate_verdicts(
                    model, odds_1x2, odds_ou25, dcs,
                    moratoriums=moratoriums,
                    multi_rule_active=multi_rule,
                )

                if verdicts:
                    for v in verdicts:
                        save_signal({
                            "fixture_id":    fixture_id,
                            "kickoff_utc":   kickoff,
                            "home_team":     home_name,
                            "away_team":     away_name,
                            "market":        v.get("market", ""),
                            "outcome":       v.get("label", v.get("market","")),
                            "model_prob":    v.get("model_prob", 0),
                            "demargin_prob": v.get("demargin_prob", 0),
                            "raw_odds":      v.get("raw_odds", 0),
                            "edge":          v.get("edge", 0),
                            "max_stake_pct": v.get("max_stake_pct", 0),
                            "status":        v.get("status", ""),
                            "dcs_score":     dcs,
                            "xg_home":       xg_home,
                            "xg_away":       xg_away,
                            "odds_source":   odds_1x2.get("source","reference"),
                        })
                    total_signals += len(verdicts)
                else:
                    save_no_bet(fixture_id, home_name, away_name,
                                kickoff, dcs, xg_home, xg_away)

                msg = format_verdict_telegram(
                    home_name, away_name, kickoff, model, verdicts, dcs,
                    n_inj_home=n_inj_home, n_inj_away=n_inj_away,
                    odds_source=odds_1x2.get("source","reference"),
                    rules_active=rules_active, moratoriums=moratoriums,
                )
                await send_channel(msg)

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"Erreur {home_name} vs {away_name}: {e}")
                logger.error(f"TRACEBACK COMPLET:\n{tb}")

        log_pipeline_run(len(fixtures), total_signals)
        logger.info(f"=== PIPELINE TERMINE === {total_signals} signaux")

    except Exception as e:
        logger.error(f"Pipeline erreur critique: {e}")


async def run_resolver():
    try:
        from storage.result_resolver import resolve_pending
        stats = await resolve_pending()
        if stats and stats.get("resolved", 0) > 0:
            await send_channel(
                "APEX-EPL BILAN\n"
                f"Resolus  : {stats['resolved']}\n"
                f"Victoires: {stats['wins']} | Defaites: {stats['losses']}\n"
                f"Win rate : {stats['win_rate']:.1%}\n"
                f"P&L      : {stats['pnl_pct']:+.1%}"
            )
    except Exception as e:
        logger.error(f"Resolver erreur: {e}")


# ── Commandes Telegram ────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_start
    await handle_start(ctx.bot, update.effective_chat.id)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_help
    await handle_help(ctx.bot, update.effective_chat.id)

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

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from interfaces.telegram_commands import handle_refresh
    await handle_refresh(ctx.bot, update.effective_chat.id)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("=== APEX-OMEGA-EPL Bot starting ===")
    logger.info(f"Data directory: {DATA_DIR}")

    from storage.signals_repo import init_db
    init_db()

    # Effacer les anciens webhooks / sessions conflictuelles
    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook efface, demarrage polling propre")
    except Exception as e:
        logger.warning(f"delete_webhook: {e}")

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_pipeline, "cron", hour="8,14,20", minute=0)
    scheduler.add_job(run_resolver, "cron", hour=23, minute=0)
    scheduler.start()

    # Application Telegram
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("bilan",   cmd_bilan))
    app.add_handler(CommandHandler("api",     cmd_api))
    app.add_handler(CommandHandler("refresh", cmd_refresh))

    await app.initialize()
    await app.start()

    # Polling avec gestion du conflit
    try:
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        logger.info("Polling actif")
    except Conflict:
        logger.warning("Conflit polling — autre instance detectee, attente 15s...")
        await asyncio.sleep(15)
        await app.updater.start_polling(drop_pending_updates=True)

    # Message de demarrage
    await send_channel(
        "APEX-OMEGA-EPL v1.3 operationnel\n"
        f"Date: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "Commandes: /start /help /status /analyse /bilan /api /refresh"
    )

    # Analyse immediate
    await run_pipeline()

    # Boucle
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
# v1.3.1 — force redeploy
