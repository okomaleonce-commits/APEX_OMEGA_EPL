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

# ── Tier mapping ───────────────────────────────────────────────────
TEAM_TIERS = {
    "Arsenal": 1, "Manchester City": 1, "Liverpool": 1,
    "Chelsea": 2, "Newcastle United": 2, "Manchester United": 2, "Tottenham": 2,
    "Aston Villa": 3, "Brighton": 3, "Fulham": 3,
    "West Ham": 3, "Bournemouth": 3, "West Ham United": 3,
    "Brentford": 4, "Crystal Palace": 4, "Everton": 4, "Wolverhampton Wanderers": 4,
    "Burnley": 5, "Sunderland": 5, "Leeds United": 5, "Nottingham Forest": 5,
}

def get_tier(name):
    for k, v in TEAM_TIERS.items():
        if k.lower() in name.lower() or name.lower() in k.lower():
            return v
    return 4


async def run_pipeline():
    logger.info("=== PIPELINE DEMARRE ===")

    from ingestion.fixtures_service import (
        fetch_upcoming, fetch_team_stats, fetch_injuries, fetch_h2h
    )
    from ingestion.odds_service import get_odds_for_match, get_reference_odds
    from models.dixon_coles import compute_xg, run_simulation
    from rules.rule_engine import apply_all_rules
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
        ais_home   = max(-0.25, -0.05 * min(n_inj_home, 5))
        ais_away   = max(-0.25, -0.05 * min(n_inj_away, 5))

        # DCS
        dcs = 55
        if home_stats.get("matches_played", 0) > 0: dcs += 10
        if away_stats.get("matches_played", 0) > 0: dcs += 10
        if odds_1x2.get("source") == "api_football": dcs += 15
        else:                                         dcs += 5
        if h2h:                                       dcs += 5
        if n_inj_home > 0 or n_inj_away > 0:         dcs += 5

        # xG de base
        xg_home, xg_away = compute_xg(
            home_stats, away_stats,
            home_capacity=fix.get("venue_capacity", 0),
            ais_f_home=ais_home,
            ais_f_away=ais_away,
        )

        # Probabilités initiales
        model_base = run_simulation(xg_home, xg_away)
        probs_base = {
            "home": model_base["home"],
            "draw": model_base["draw"],
            "away": model_base["away"],
        }

        # Contexte pour les règles
        match_ctx = {
            "home_name":  home_name,
            "away_name":  away_name,
            "home_tier":  get_tier(home_name),
            "away_tier":  get_tier(away_name),
            "home_stake": 5,   # défaut mid-table — à enrichir
            "away_stake": 5,
            "h2h_matches": h2h,
            "home_lineup": None,
            "away_lineup": None,
            "home_te_goals": 0,
            "home_te_hours": 999,
            "away_te_goals": 0,
            "away_te_hours": 999,
            "away_eba_active":    False,
            "away_eba_victory":   False,
            "away_eba_rotations": 0,
            "home_raw_odds": odds_1x2.get("home_raw", 2.0),
            "away_raw_odds": odds_1x2.get("away_raw", 2.0),
            "home_stats":    home_stats,
            "away_stats":    away_stats,
            "home_ais_f_raw": ais_home,
            "away_ais_f_raw": ais_away,
        }

        # Appliquer les 11 règles
        probs_adj, moratoriums, rules_active, xg_home_adj, xg_away_adj, multi_rule = (
            apply_all_rules(match_ctx, probs_base, xg_home, xg_away)
        )

        # Recalculer le modèle complet avec xG ajustés
        model = run_simulation(xg_home_adj, xg_away_adj)
        # Surcharger les probs 1X2 avec les probs ajustées par les règles
        model["home"] = probs_adj["home"]
        model["draw"] = probs_adj["draw"]
        model["away"] = probs_adj["away"]

        # Verdicts
        prefer_dnb = any("R9_ACL_HOME" in r for r in rules_active)
        verdicts = generate_verdicts(
            model, odds_1x2, odds_ou25, dcs,
            moratoriums=moratoriums,
            multi_rule_active=multi_rule,
            prefer_dnb=prefer_dnb,
        )

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
                    "xg_home":       xg_home_adj,
                    "xg_away":       xg_away_adj,
                    "odds_source":   odds_1x2.get("source", "reference"),
                })
            total_signals += len(verdicts)
        else:
            save_no_bet(fixture_id, home_name, away_name,
                        kickoff, dcs, xg_home_adj, xg_away_adj)

        msg = format_verdict_telegram(
            home_name, away_name, kickoff, model, verdicts, dcs,
            n_inj_home=n_inj_home,
            n_inj_away=n_inj_away,
            odds_source=odds_1x2.get("source", "reference"),
            rules_active=rules_active,
            moratoriums=moratoriums,
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
            f"Signaux resolus : {stats['resolved']}\n"
            f"Victoires       : {stats['wins']}\n"
            f"Defaites        : {stats['losses']}\n"
            f"Win rate        : {stats['win_rate']:.1%}\n"
            f"P&L cumule      : {stats['pnl_pct']:+.1%}"
        )
        await send_telegram(msg)


async def send_telegram(text):
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    try:
        bot = Bot(token=BOT_TOKEN)
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=CHANNEL_ID, text=text[i:i+4000])
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
        "Etape 4 — 11 regles contextuelles v1.3 actives"
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
