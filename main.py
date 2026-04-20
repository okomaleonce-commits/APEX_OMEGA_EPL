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


def build_rule_context(fix, home_inj, away_inj, h2h, home_tier, away_tier):
    """
    Construit le contexte des regles v1.3 a partir des donnees disponibles.
    Les champs UCL/CCR/TE/EBA sont a 0 par defaut — 
    seront remplis quand les sources correspondantes seront integrees.
    """
    # H2H avg goals
    h2h_avg = 0.0
    if h2h:
        h2h_avg = sum(m.get("total_goals", 0) for m in h2h) / len(h2h)

    # ACL check simplifie : > 3 blesses = potentiellement ACL
    home_acl = len(home_inj) >= 3
    away_acl = len(away_inj) >= 3
    home_acl_factor = 2.0 if len(home_inj) >= 4 else 1.5
    away_acl_factor = 2.0 if len(away_inj) >= 4 else 1.5

    # Enjeux : estimation par tier et position saison
    home_stake = 6 if home_tier <= 2 else (8 if home_tier == 5 else 3)
    away_stake = 6 if away_tier <= 2 else (8 if away_tier == 5 else 3)

    return {
        "home_tier":           home_tier,
        "away_tier":           away_tier,
        "venue_capacity":      fix.get("venue_capacity", 0),
        "home_fatigue_coeff":  1.0,
        "away_fatigue_coeff":  1.0,
        "home_inj_count":      len(home_inj),
        "away_inj_count":      len(away_inj),
        "home_acl_active":     home_acl,
        "away_acl_active":     away_acl,
        "home_acl_factor":     home_acl_factor,
        "away_acl_factor":     away_acl_factor,
        "te_home":             False,
        "te_away":             False,
        "te_goals":            0,
        "eba_away":            False,
        "eba_home":            False,
        "h2h_avg_goals":       round(h2h_avg, 2),
        "home_ccr_ratio":      1.0,
        "away_ccr_ratio":      1.0,
        "home_stake_score":    home_stake,
        "away_stake_score":    away_stake,
    }


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

        # AIS-F : -5% par blesse (max -25%)
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

        # Tiers
        home_tier = get_tier(home_name)
        away_tier = get_tier(away_name)

        # xG Dixon-Coles
        xg_home, xg_away = compute_xg(
            home_stats, away_stats,
            home_capacity=fix.get("venue_capacity", 0),
            ais_f_home=ais_home,
            ais_f_away=ais_away,
        )

        # Simulation Monte-Carlo
        model = run_simulation(xg_home, xg_away)
        probs = {"home": model["home"], "draw": model["draw"], "away": model["away"]}

        # ── APPLICATION DES 11 REGLES v1.3 ───────────────────────
        rule_ctx = build_rule_context(
            fix, home_inj, away_inj, h2h, home_tier, away_tier
        )
        adj_probs, moratoriums, rules_active = apply_all_rules(
            home_name, away_name, probs, rule_ctx
        )

        # Mettre a jour le modele avec les probs ajustees
        model["home"] = adj_probs.get("home", model["home"])
        model["draw"] = adj_probs.get("draw", model["draw"])
        model["away"] = adj_probs.get("away", model["away"])

        # Moratoriums bloques
        blocked_markets = {m["market"] for m in moratoriums}

        # Verdicts
        verdicts = generate_verdicts(
            model, odds_1x2, odds_ou25, dcs,
            moratoriums=list(blocked_markets)
        )

        # R15 : seuil reduit si >= 2 regles v1.3 actives
        rules_v13 = [r for r in rules_active
                     if any(t in r for t in
                            ["R6_","R7_","R8_","R9_","R10_","R11_"])]
        if len(rules_v13) >= 2 and "R15_MULTI_RULE" in " ".join(rules_active):
            for v in verdicts:
                v["r15_active"] = True

        # Sauvegarder SQLite
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

        # Telegram
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
        "APEX-OMEGA-EPL v1.3 demarre\n"
        f"Date: {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC\n"
        "11 regles contextuelles actives | SQLite | Dixon-Coles"
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
