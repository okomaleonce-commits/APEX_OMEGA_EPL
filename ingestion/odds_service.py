import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY_AF = os.getenv("FOOTBALL_DATA_API_KEY", "")
DATA_DIR   = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR  = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EPL_LEAGUE_ID = os.getenv("EPL_LEAGUE_ID", "39")
EPL_SEASON    = os.getenv("EPL_SEASON", "2025")

API_HOST = "v3.football.api-sports.io"
HEADERS  = {
    "x-rapidapi-key":  API_KEY_AF,
    "x-rapidapi-host": API_HOST,
}

# Bookmaker IDs API-Football
# 8 = Bet365 | 1 = Bwin | 6 = Betway
BOOKMAKER_ID = 8


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"odds_{name}.json"


def _is_fresh(path: Path, ttl_hours: float = 3) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def fetch_all_epl_odds() -> list[dict]:
    """Récupère les cotes EPL via API-Football."""
    cache = _cache_path("all_epl")

    if _is_fresh(cache):
        logger.debug("Cotes depuis cache")
        with open(cache) as f:
            return json.load(f)

    if not API_KEY_AF:
        logger.warning("FOOTBALL_DATA_API_KEY non configurée")
        return []

    try:
        resp = requests.get(
            f"https://{API_HOST}/odds",
            headers=HEADERS,
            params={
                "league":    EPL_LEAGUE_ID,
                "season":    EPL_SEASON,
                "bookmaker": BOOKMAKER_ID,
                "next":      10,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw_list = resp.json().get("response", [])

        results = []
        for item in raw_list:
            parsed = _parse_odds_item(item)
            if parsed:
                results.append(parsed)

        with open(cache, "w") as f:
            json.dump(results, f, indent=2)

        logger.info(f"{len(results)} matchs avec cotes (API-Football)")
        return results

    except Exception as e:
        logger.error(f"Erreur cotes API-Football: {e}")
        if cache.exists():
            with open(cache) as f:
                return json.load(f)
        return []


def _parse_odds_item(item: dict) -> dict | None:
    """Parse un item de cotes API-Football."""
    fix   = item.get("fixture", {})
    teams = item.get("teams", {})
    home  = teams.get("home", {}).get("name", "")
    away  = teams.get("away", {}).get("name", "")

    if not home or not away:
        return None

    odds_1x2 = {}
    odds_ou25 = {}

    for bm in item.get("bookmakers", []):
        for bet in bm.get("bets", []):
            name = bet.get("name", "")

            # 1X2
            if name == "Match Winner":
                for v in bet.get("values", []):
                    val  = v.get("value", "")
                    odd  = _safe_float(v.get("odd", 0))
                    if val == "Home": odds_1x2["home_raw"] = odd
                    elif val == "Draw": odds_1x2["draw_raw"] = odd
                    elif val == "Away": odds_1x2["away_raw"] = odd

            # Over/Under 2.5
            elif name == "Goals Over/Under":
                for v in bet.get("values", []):
                    val = v.get("value", "")
                    odd = _safe_float(v.get("odd", 0))
                    if val == "Over 2.5":  odds_ou25["over_raw"]  = odd
                    if val == "Under 2.5": odds_ou25["under_raw"] = odd

    # Démarginiser 1X2
    if len(odds_1x2) == 3:
        odds_1x2 = _demarginize_1x2(odds_1x2)
    else:
        odds_1x2 = {}

    # Démarginiser Over/Under
    if len(odds_ou25) == 2:
        odds_ou25 = _demarginize_ou(odds_ou25)
    else:
        odds_ou25 = {}

    return {
        "home_team":   home,
        "away_team":   away,
        "kickoff_utc": fix.get("date", ""),
        "fixture_id":  fix.get("id", 0),
        "odds_
