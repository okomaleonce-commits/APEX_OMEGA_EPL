"""
Resout automatiquement les signaux passes en fetchant
les resultats via API-Football.
"""
import os
import logging
import requests
from datetime import datetime, timezone

from storage.signals_repo import (
    get_unresolved_signals, resolve_signal,
    save_outcome, get_stats
)

logger = logging.getLogger(__name__)

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
API_HOST = "v3.football.api-sports.io"
HEADERS  = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": API_HOST}


def fetch_result(fixture_id):
    if not API_KEY or not fixture_id:
        return None
    try:
        resp = requests.get(
            f"https://{API_HOST}/fixtures",
            headers=HEADERS,
            params={"id": fixture_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("response", [])
        if not data:
            return None
        fix    = data[0]
        status = fix["fixture"]["status"]["short"]
        if status not in ("FT", "AET", "PEN"):
            return None
        goals = fix["goals"]
        return {
            "home_goals": goals.get("home", 0) or 0,
            "away_goals": goals.get("away", 0) or 0,
            "status":     status,
        }
    except Exception as e:
        logger.error(f"Erreur fetch resultat {fixture_id}: {e}")
        return None


def resolve_signal_result(signal, result):
    """
    Determine WIN ou LOSS selon le marche et le resultat.
    """
    market = signal.get("market", "")
    hg     = result["home_goals"]
    ag     = result["away_goals"]
    total  = hg + ag
    odds   = signal.get("raw_odds", 1)

    outcome = None

    if market == "1x2_home":
        outcome = "WIN" if hg > ag else "LOSS"
    elif market == "1x2_draw":
        outcome = "WIN" if hg == ag else "LOSS"
    elif market == "1x2_away":
        outcome = "WIN" if ag > hg else "LOSS"
    elif market == "over25":
        outcome = "WIN" if total > 2 else "LOSS"
    elif market == "under25":
        outcome = "WIN" if total <= 2 else "LOSS"
    elif market == "btts_yes":
        outcome = "WIN" if (hg > 0 and ag > 0) else "LOSS"
    elif market == "btts_no":
        outcome = "WIN" if (hg == 0 or ag == 0) else "LOSS"

    if outcome is None:
        return

    stake = signal.get("max_stake_pct", 0.05)
    if outcome == "WIN":
        pnl = round(stake * (odds - 1), 4)
    else:
        pnl = round(-stake, 4)

    resolve_signal(signal["id"], outcome, pnl)
    logger.info(
        f"Signal #{signal['id']} resolu: {outcome} | "
        f"{signal['home_team']} vs {signal['away_team']} | "
        f"{market} | P&L: {pnl:+.1%}"
    )
    return outcome, pnl


async def resolve_pending():
    """
    Resout tous les signaux en attente de resolution.
    Appele par le scheduler quotidien.
    """
    signals = get_unresolved_signals()
    if not signals:
        logger.info("Aucun signal a resoudre")
        return

    logger.info(f"{len(signals)} signaux a resoudre")
    resolved_count = 0

    for signal in signals:
        fixture_id = signal.get("fixture_id")
        result = fetch_result(fixture_id)

        if not result:
            logger.debug(f"Resultat non disponible pour fixture {fixture_id}")
            continue

        # Sauvegarder le resultat du match
        save_outcome(
            fixture_id,
            signal["home_team"],
            signal["away_team"],
            signal["kickoff_utc"],
            result["home_goals"],
            result["away_goals"],
        )

        r = resolve_signal_result(signal, result)
        if r:
            resolved_count += 1

    stats = get_stats()
    logger.info(
        f"Resolution terminee: {resolved_count} signaux. "
        f"Stats: {stats['wins']}W/{stats['losses']}L | "
        f"P&L: {stats['pnl_pct']:+.1%} | "
        f"Win rate: {stats['win_rate']:.1%}"
    )
    return stats
