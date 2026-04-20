import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ODDS_API_KEY        = os.getenv("ODDS_API_KEY", "")
ODDS_API_BOOKMAKERS = os.getenv("ODDS_API_BOOKMAKERS", "pinnacle,betfair_ex_eu,bet365")

DATA_DIR  = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT    = "soccer_epl"

SHARP_ORDER = ["pinnacle", "betfair_ex_eu", "bet365", "unibet", "bwin"]


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"odds_{name}.json"


def _is_fresh(path: Path, ttl_hours: float = 2) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def fetch_all_epl_odds() -> list[dict]:
    """Récupère les cotes via API-Football (fallback gratuit)."""
    cache = _cache_path("all_epl")

    if _is_fresh(cache):
        with open(cache) as f:
            return json.load(f)

    API_KEY_AF = os.getenv("FOOTBALL_DATA_API_KEY", "")
    if not API_KEY_AF:
        return []

    try:
        resp = requests.get(
            "https://v3.football.api-sports.io/odds",
            headers={
                "x-rapidapi-key":  API_KEY_AF,
                "x-rapidapi-host": "v3.football.api-sports.io",
            },
            params={
                "league":     os.getenv("EPL_LEAGUE_ID", "39"),
                "season":     os.getenv("EPL_SEASON", "2025"),
                "bookmaker":  8,   # Bet365 (ID API-Football)
                "next":       10,
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = []
        for item in resp.json().get("response", []):
            fix   = item.get("fixture", {})
            teams = item.get("teams",   {})
            home  = teams.get("home", {}).get("name", "")
            away  = teams.get("away", {}).get("name", "")

            odds_1x2 = {}
            for bm in item.get("bookmakers", []):
                for bet in bm.get("bets", []):
                    if bet.get("name") == "Match Winner":
                        for v in bet.get("values", []):
                            if v["value"] == "Home":
                                odds_1x2["home_raw"] = float(v["odd"])
                            elif v["value"] == "Draw":
                                odds_1x2["draw_raw"] = float(v["odd"])
                            elif v["value"] == "Away":
                                odds_1x2["away_raw"] = float(v["odd"])

            if len(odds_1x2) == 3:
                total = sum(1/v for v in odds_1x2.values())
                odds_1x2.update({
                    "home_prob":  round((1/odds_1x2["home_raw"]) / total, 4),
                    "draw_prob":  round((1/odds_1x2["draw_raw"]) / total, 4),
                    "away_prob":  round((1/odds_1x2["away_raw"]) / total, 4),
                    "margin":     round(total - 1.0, 4),
                })

            results.append({
                "home_team":   home,
                "away_team":   away,
                "kickoff_utc": fix.get("date", ""),
                "odds_1x2":    odds_1x2,
                "bookmaker":   "bet365",
            })

        with open(cache, "w") as f:
            json.dump(results, f, indent=2)

        logger.info(f"{len(results)} matchs avec cotes API-Football")
        return results

    except Exception as e:
        logger.error(f"Erreur cotes API-Football: {e}")
        return []


def get_odds_for_match(home_team: str, away_team: str) -> dict:
    """
    Trouve les cotes pour un match spécifique.
    Retourne un dict avec les cotes 1X2, Over/Under, démarginisées.
    """
    all_odds = fetch_all_epl_odds()

    home_lower = home_team.lower()
    away_lower = away_team.lower()

    for game in all_odds:
        h = game.get("home_team", "").lower()
        a = game.get("away_team", "").lower()

        # Matching flexible (gère les noms légèrement différents)
        if _name_match(home_lower, h) and _name_match(away_lower, a):
            return game

    logger.warning(f"Cotes non trouvées pour {home_team} vs {away_team}")
    return {}


def _name_match(a: str, b: str) -> bool:
    """Matching souple entre noms d'équipes."""
    a_words = set(a.split())
    b_words = set(b.split())
    common  = a_words & b_words
    return len(common) >= 1 or a in b or b in a


def _parse_game(game: dict) -> dict | None:
    """Parse un match brut de The Odds API."""
    home = game.get("home_team", "")
    away = game.get("away_team", "")

    if not home or not away:
        return None

    result = {
        "home_team":   home,
        "away_team":   away,
        "kickoff_utc": game.get("commence_time", ""),
        "odds_1x2":    {},
        "odds_ou25":   {},
        "bookmaker":   None,
        "margin":      None,
    }

    bookmakers = game.get("bookmakers", [])

    # Chercher dans l'ordre de priorité (bookmaker le plus sharp en premier)
    for target in SHARP_ORDER:
        for bm in bookmakers:
            if bm.get("key", "").lower() == target:
                result["bookmaker"] = target

                for market in bm.get("markets", []):
                    key = market.get("key", "")

                    if key == "h2h":
                        odds = _extract_1x2(market.get("outcomes", []), home, away)
                        if odds:
                            result["odds_1x2"] = _demarginize_1x2(odds)

                    elif key == "totals":
                        ou = _extract_ou25(market.get("outcomes", []))
                        if ou:
                            result["odds_ou25"] = _demarginize_ou(ou)

                if result["odds_1x2"]:
                    # Calculer la marge du bookmaker
                    probs = [
                        1 / result["odds_1x2"]["home_raw"],
                        1 / result["odds_1x2"]["draw_raw"],
                        1 / result["odds_1x2"]["away_raw"],
                    ]
                    result["margin"] = round(sum(probs) - 1.0, 4)
                    return result

    return result if result["odds_1x2"] else None


def _extract_1x2(outcomes: list, home: str, away: str) -> dict | None:
    """Extrait les cotes 1X2 brutes."""
    odds = {}
    for o in outcomes:
        name  = o.get("name", "")
        price = o.get("price", 0)
        if price <= 1:
            continue
        if name == home:
            odds["home_raw"] = price
        elif name == away:
            odds["away_raw"] = price
        elif name.lower() == "draw":
            odds["draw_raw"] = price

    if len(odds) == 3:
        return odds
    return None


def _extract_ou25(outcomes: list) -> dict | None:
    """Extrait les cotes Over/Under 2.5."""
    for o in outcomes:
        point = o.get("point", 0)
        if abs(point - 2.5) < 0.01:
            return {
                o["name"].lower(): o["price"]
                for o in outcomes
                if abs(o.get("point", 0) - 2.5) < 0.01
            }
    return None


def _demarginize_1x2(odds: dict) -> dict:
    """
    Démarginise les cotes 1X2 par normalisation.
    Retourne les probabilités réelles estimées.
    """
    h  = odds["home_raw"]
    d  = odds["draw_raw"]
    a  = odds["away_raw"]

    total = (1/h) + (1/d) + (1/a)

    return {
        "home_raw":    round(h, 3),
        "draw_raw":    round(d, 3),
        "away_raw":    round(a, 3),
        "home_prob":   round((1/h) / total, 4),
        "draw_prob":   round((1/d) / total, 4),
        "away_prob":   round((1/a) / total, 4),
        "margin":      round(total - 1.0, 4),
    }


def _demarginize_ou(ou: dict) -> dict:
    """Démarginise les cotes Over/Under."""
    over  = ou.get("over",  0)
    under = ou.get("under", 0)

    if not over or not under:
        return ou

    total = (1/over) + (1/under)
    return {
        "over_raw":   round(over,  3),
        "under_raw":  round(under, 3),
        "over_prob":  round((1/over)  / total, 4),
        "under_prob": round((1/under) / total, 4),
        "margin":     round(total - 1.0, 4),
    }
