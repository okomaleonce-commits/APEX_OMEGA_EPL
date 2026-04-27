import os
import json
import math
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY_AF    = os.getenv("FOOTBALL_DATA_API_KEY", "") or os.getenv("API_KEY", "")
DATA_DIR      = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR     = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EPL_LEAGUE_ID = os.getenv("EPL_LEAGUE_ID", "39")
EPL_SEASON    = os.getenv("EPL_SEASON", "2025")
API_HOST      = "v3.football.api-sports.io"
HEADERS       = {"x-apisports-key": API_KEY_AF}

BOOKMAKER_IDS = [8, 1, 6, 11]   # Bet365, Bwin, Betway, 1xBet


def _cache_path(name):
    return CACHE_DIR / f"odds_{name}.json"


def _is_fresh(path, ttl_hours=3):
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def fetch_odds_for_fixture(fixture_id: int) -> dict:
    """
    Recupere les cotes pour un fixture precis via son ID.
    Essaie plusieurs bookmakers en cascade.
    Retourne {} si aucune cote disponible.
    """
    if not fixture_id:
        return {}

    cache = _cache_path(f"fixture_{fixture_id}")
    if _is_fresh(cache, ttl_hours=2):
        with open(cache) as f:
            return json.load(f)

    if not API_KEY_AF:
        return {}

    for bk_id in BOOKMAKER_IDS:
        try:
            resp = requests.get(
                f"https://{API_HOST}/odds",
                headers=HEADERS,
                params={
                    "fixture":   fixture_id,
                    "bookmaker": bk_id,
                    "season":    EPL_SEASON,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            errors = data.get("errors", {})
            if errors:
                logger.warning(f"API odds fixture={fixture_id} bk={bk_id}: {errors}")
                continue
            items = data.get("response", [])
            if not items:
                continue
            parsed = _parse_odds_item(items[0])
            if parsed and parsed.get("odds_1x2"):
                with open(cache, "w") as f:
                    json.dump(parsed, f, indent=2)
                logger.info(f"Cotes fixture {fixture_id} via bookmaker {bk_id}")
                return parsed
        except Exception as e:
            logger.warning(f"Odds fixture={fixture_id} bk={bk_id}: {e}")

    logger.warning(f"Pas de cotes pour fixture {fixture_id} (trop loin ou API invalide)")
    return {}


def fetch_all_epl_odds():
    """Legacy — retourne liste vide. Utiliser fetch_odds_for_fixture() a la place."""
    return []


def _parse_odds_item(item):
    fix   = item.get("fixture", {})
    teams = item.get("teams", {})
    home  = teams.get("home", {}).get("name", "")
    away  = teams.get("away", {}).get("name", "")
    if not home or not away:
        return None

    odds_1x2  = {}
    odds_ou25 = {}

    for bm in item.get("bookmakers", []):
        for bet in bm.get("bets", []):
            name = bet.get("name", "")
            if name == "Match Winner":
                for v in bet.get("values", []):
                    val = v.get("value", "")
                    odd = _safe_float(v.get("odd", 0))
                    if val == "Home":      odds_1x2["home_raw"] = odd
                    elif val == "Draw":    odds_1x2["draw_raw"] = odd
                    elif val == "Away":    odds_1x2["away_raw"] = odd
            elif name == "Goals Over/Under":
                for v in bet.get("values", []):
                    val = v.get("value", "")
                    odd = _safe_float(v.get("odd", 0))
                    if val == "Over 2.5":    odds_ou25["over_raw"]  = odd
                    elif val == "Under 2.5": odds_ou25["under_raw"] = odd

    if len(odds_1x2) == 3:
        odds_1x2 = _demarginize_1x2(odds_1x2)
    else:
        odds_1x2 = {}

    if len(odds_ou25) == 2:
        odds_ou25 = _demarginize_ou(odds_ou25)
    else:
        odds_ou25 = {}

    return {
        "home_team":   home,
        "away_team":   away,
        "kickoff_utc": fix.get("date", ""),
        "fixture_id":  fix.get("id", 0),
        "odds_1x2":    odds_1x2,
        "odds_ou25":   odds_ou25,
    }


def get_odds_for_match(home_team, away_team):
    """Legacy name-based lookup — fallback uniquement."""
    logger.warning(f"Cotes API non trouvees: {home_team} vs {away_team}")
    return {}


def get_reference_odds(home_team, away_team, home_stats, away_stats):
    avg  = 1.445
    gf_h = home_stats.get("goals_scored_avg",  avg)
    ga_h = home_stats.get("goals_conceded_avg", avg)
    gf_a = away_stats.get("goals_scored_avg",  avg)
    ga_a = away_stats.get("goals_conceded_avg", avg)

    xg_h = max((gf_h / avg) * (ga_a / avg) * avg * 1.08, 0.3)
    xg_a = max((gf_a / avg) * (ga_h / avg) * avg, 0.2)

    hw, dr, aw = _poisson_1x2(xg_h, xg_a)
    total  = hw + dr + aw
    margin = 0.05

    home_raw = round(1 / ((hw / total) * (1 + margin)), 2)
    draw_raw = round(1 / ((dr / total) * (1 + margin)), 2)
    away_raw = round(1 / ((aw / total) * (1 + margin)), 2)
    t = (1 / home_raw) + (1 / draw_raw) + (1 / away_raw)

    over_p    = _poisson_over25(xg_h, xg_a)
    under_p   = 1 - over_p
    over_raw  = round(1 / (over_p  * (1 + margin)), 2)
    under_raw = round(1 / (under_p * (1 + margin)), 2)

    logger.info(
        f"Cotes reference: {home_team} vs {away_team} | "
        f"xG {xg_h:.2f}/{xg_a:.2f} | "
        f"{home_raw}/{draw_raw}/{away_raw}"
    )

    odds_1x2 = {
        "home_raw":  home_raw, "draw_raw":  draw_raw, "away_raw":  away_raw,
        "home_prob": round((1 / home_raw) / t, 4),
        "draw_prob": round((1 / draw_raw) / t, 4),
        "away_prob": round((1 / away_raw) / t, 4),
        "margin":    round(t - 1.0, 4),
        "source":    "reference_model",
    }
    odds_ou25 = {
        "over_raw":   over_raw,  "under_raw":  under_raw,
        "over_prob":  round(over_p,  4),
        "under_prob": round(under_p, 4),
        "source":     "reference_model",
    }
    return odds_1x2, odds_ou25


def _poisson_1x2(mu_h, mu_a, max_g=8):
    hw = dr = aw = 0.0
    for h in range(max_g):
        for a in range(max_g):
            p = (math.exp(-mu_h) * mu_h ** h / math.factorial(h) *
                 math.exp(-mu_a) * mu_a ** a / math.factorial(a))
            if h > a:    hw += p
            elif h == a: dr += p
            else:        aw += p
    return hw, dr, aw


def _poisson_over25(mu_h, mu_a, max_g=10):
    under = 0.0
    for h in range(max_g):
        for a in range(max_g):
            if h + a <= 2:
                under += (math.exp(-mu_h) * mu_h ** h / math.factorial(h) *
                          math.exp(-mu_a) * mu_a ** a / math.factorial(a))
    return round(1 - under, 4)


def _name_match(a, b):
    return bool(set(a.split()) & set(b.split())) or a in b or b in a


def _safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _demarginize_1x2(odds):
    h, d, a = odds["home_raw"], odds["draw_raw"], odds["away_raw"]
    if not all([h, d, a]):
        return odds
    total = (1 / h) + (1 / d) + (1 / a)
    return {
        "home_raw":  round(h, 3), "draw_raw":  round(d, 3), "away_raw":  round(a, 3),
        "home_prob": round((1 / h) / total, 4),
        "draw_prob": round((1 / d) / total, 4),
        "away_prob": round((1 / a) / total, 4),
        "margin":    round(total - 1.0, 4),
        "source":    "api_football",
    }


def _demarginize_ou(ou):
    over, under = ou.get("over_raw", 0), ou.get("under_raw", 0)
    if not over or not under:
        return ou
    total = (1 / over) + (1 / under)
    return {
        "over_raw":   round(over,  3), "under_raw":  round(under, 3),
        "over_prob":  round((1 / over)  / total, 4),
        "under_prob": round((1 / under) / total, 4),
        "source":     "api_football",
    }
