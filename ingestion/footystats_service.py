import os
import json
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

FOOTYSTATS_API_KEY = (
    os.getenv("FOOTYSTATS_API_KEY", "")
    or os.getenv("FOOTY_API_KEY", "")
    or os.getenv("FOOTYSTATS_KEY", "")
)
FOOTYSTATS_BASE_URL = os.getenv(
    "FOOTYSTATS_BASE_URL", "https://api.football-data-api.com"
).rstrip("/")
FOOTYSTATS_TIMEZONE = os.getenv("FOOTYSTATS_TIMEZONE", "UTC")
FOOTYSTATS_MAX_PAGES = int(os.getenv("FOOTYSTATS_MAX_PAGES", "2"))

DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache" / "footystats"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {"Accept": "application/json"}


def _cache_path(name):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(name))
    return CACHE_DIR / f"{safe}.json"


def _is_fresh(path, ttl_minutes=60):
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(minutes=ttl_minutes)


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"FootyStats cache save failed: {exc}")


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_probability(value):
    """Convertit 0.61, 61 ou '61%' vers une probabilité décimale 0.61."""
    if isinstance(value, str):
        value = value.replace("%", "").strip()
    v = _safe_float(value, 0.0)
    if v <= 0:
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(v, 1.0))


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_name(name):
    text = (name or "").lower()
    replacements = {
        "fc": "", "afc": "", "cf": "", "the": "",
        "man utd": "manchester united", "man united": "manchester united",
        "man city": "manchester city", "spurs": "tottenham",
        "wolves": "wolverhampton wanderers", "nottm forest": "nottingham forest",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(text.replace("-", " ").replace(".", " ").split())


def _name_score(a, b):
    a_norm = _normalize_name(a)
    b_norm = _normalize_name(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    if a_norm in b_norm or b_norm in a_norm:
        return 0.92
    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    token_score = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)
    fuzzy = SequenceMatcher(None, a_norm, b_norm).ratio()
    return max(token_score, fuzzy)


def _match_teams(home, away, candidate):
    c_home = candidate.get("home_name") or candidate.get("home_team") or candidate.get("home") or ""
    c_away = candidate.get("away_name") or candidate.get("away_team") or candidate.get("away") or ""
    direct = (_name_score(home, c_home) + _name_score(away, c_away)) / 2
    swapped = (_name_score(home, c_away) + _name_score(away, c_home)) / 2
    return max(direct, swapped), swapped > direct


def _extract_data(payload):
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], (dict, list)):
        return data["data"]
    return data


def footystats_api_call(endpoint, params=None, ttl_minutes=60, cache_key=None):
    if not FOOTYSTATS_API_KEY:
        return None
    params = dict(params or {})
    params["key"] = FOOTYSTATS_API_KEY
    endpoint = endpoint.strip("/")
    cache = _cache_path(cache_key or f"{endpoint}_{json.dumps(params, sort_keys=True)}")
    if _is_fresh(cache, ttl_minutes=ttl_minutes):
        cached = _load(cache)
        if cached is not None:
            return cached
    try:
        resp = requests.get(
            f"{FOOTYSTATS_BASE_URL}/{endpoint}",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            logger.warning(f"FootyStats API error {endpoint}: {data.get('message', data)}")
            return _load(cache)
        _save(cache, data)
        return data
    except Exception as exc:
        logger.warning(f"FootyStats call failed [{endpoint}]: {exc}")
        return _load(cache)


def get_todays_matches(date=None, timezone_name=None, page=1):
    params = {"page": page}
    if date:
        params["date"] = date
    if timezone_name or FOOTYSTATS_TIMEZONE:
        params["timezone"] = timezone_name or FOOTYSTATS_TIMEZONE
    return footystats_api_call(
        "todays-matches",
        params=params,
        ttl_minutes=30,
        cache_key=f"todays_{date or 'today'}_{params.get('timezone')}_{page}",
    )


def get_match_details(match_id):
    if not match_id:
        return None
    return footystats_api_call(
        "match",
        params={"match_id": match_id},
        ttl_minutes=30,
        cache_key=f"match_{match_id}",
    )


def get_btts_stats(season_id=None, league_id=None, country=None):
    params = {}
    if season_id:
        params["season_id"] = season_id
    if league_id:
        params["league_id"] = league_id
    if country:
        params["country"] = country
    return footystats_api_call("btts-stats", params=params, ttl_minutes=360)


def get_over_2_5_stats(season_id=None, league_id=None, country=None):
    params = {}
    if season_id:
        params["season_id"] = season_id
    if league_id:
        params["league_id"] = league_id
    if country:
        params["country"] = country
    return footystats_api_call("over-2.5-stats", params=params, ttl_minutes=360)


def _demarginize_2way(over, under, source):
    over = _safe_float(over)
    under = _safe_float(under)
    if over <= 1.01 or under <= 1.01:
        return {}
    total = (1 / over) + (1 / under)
    return {
        "over_raw": round(over, 3),
        "under_raw": round(under, 3),
        "over_prob": round((1 / over) / total, 4),
        "under_prob": round((1 / under) / total, 4),
        "margin": round(total - 1.0, 4),
        "source": source,
    }


def _demarginize_btts(yes, no, source):
    yes = _safe_float(yes)
    no = _safe_float(no)
    if yes <= 1.01 or no <= 1.01:
        return {}
    total = (1 / yes) + (1 / no)
    return {
        "yes_raw": round(yes, 3),
        "no_raw": round(no, 3),
        "yes_prob": round((1 / yes) / total, 4),
        "no_prob": round((1 / no) / total, 4),
        "margin": round(total - 1.0, 4),
        "source": source,
    }


def _demarginize_1x2(home, draw, away, source):
    home = _safe_float(home)
    draw = _safe_float(draw)
    away = _safe_float(away)
    if home <= 1.01 or draw <= 1.01 or away <= 1.01:
        return {}
    total = (1 / home) + (1 / draw) + (1 / away)
    return {
        "home_raw": round(home, 3),
        "draw_raw": round(draw, 3),
        "away_raw": round(away, 3),
        "home_prob": round((1 / home) / total, 4),
        "draw_prob": round((1 / draw) / total, 4),
        "away_prob": round((1 / away) / total, 4),
        "margin": round(total - 1.0, 4),
        "source": source,
    }


def _parse_match_details(payload, fallback=None):
    data = _extract_data(payload)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = {}
    fallback = fallback or {}

    home = data.get("home_name") or data.get("home_team") or fallback.get("home_name") or fallback.get("home_team")
    away = data.get("away_name") or data.get("away_team") or fallback.get("away_name") or fallback.get("away_team")
    match_id = data.get("id") or data.get("match_id") or fallback.get("id") or fallback.get("match_id")

    btts = _as_probability(
        data.get("btts_potential")
        or data.get("btts_percentage")
        or data.get("btts_percent")
        or fallback.get("btts_potential")
    )
    over25 = _as_probability(
        data.get("o25_potential")
        or data.get("over25_potential")
        or data.get("over_2_5_percentage")
        or fallback.get("o25_potential")
        or fallback.get("over25_potential")
    )
    avg_goals = _safe_float(
        data.get("avg_potential")
        or data.get("seasonAVG_overall")
        or data.get("average_goals")
        or fallback.get("avg_potential"),
        0.0,
    )

    odds_ou25 = _demarginize_2way(
        data.get("odds_ft_over25") or data.get("odds_ft_over2_5") or data.get("odds_over25"),
        data.get("odds_ft_under25") or data.get("odds_ft_under2_5") or data.get("odds_under25"),
        "footystats",
    )
    odds_btts = _demarginize_btts(
        data.get("odds_btts_yes"),
        data.get("odds_btts_no"),
        "footystats",
    )
    odds_1x2 = _demarginize_1x2(
        data.get("odds_ft_1") or data.get("odds_home"),
        data.get("odds_ft_x") or data.get("odds_draw"),
        data.get("odds_ft_2") or data.get("odds_away"),
        "footystats",
    )

    return {
        "status": "ok" if (btts or over25 or avg_goals or odds_ou25 or odds_btts or odds_1x2) else "empty",
        "source": "footystats",
        "match_id": _safe_int(match_id),
        "home_name": home or "",
        "away_name": away or "",
        "btts_prob": round(btts, 4),
        "over25_prob": round(over25, 4),
        "avg_goals": round(avg_goals, 3),
        "odds_1x2": odds_1x2,
        "odds_ou25": odds_ou25,
        "odds_btts": odds_btts,
    }


def find_footystats_match(home_team, away_team, kickoff_utc=None, timezone_name=None):
    """
    Retourne une couche d'enrichissement FootyStats pour un match donné.
    Échoue en silence si la clé n'est pas configurée ou si le match n'est pas trouvé.
    """
    if not FOOTYSTATS_API_KEY:
        return {}

    date = None
    if kickoff_utc:
        try:
            date = str(kickoff_utc)[:10]
        except Exception:
            date = None

    best = None
    best_score = 0.0
    for page in range(1, FOOTYSTATS_MAX_PAGES + 1):
        payload = get_todays_matches(date=date, timezone_name=timezone_name, page=page)
        data = _extract_data(payload)
        if isinstance(data, dict):
            candidates = data.get("matches") or data.get("fixtures") or data.get("data") or []
        else:
            candidates = data or []
        if not isinstance(candidates, list):
            candidates = []

        for candidate in candidates:
            score, _ = _match_teams(home_team, away_team, candidate)
            if score > best_score:
                best_score = score
                best = candidate

        if len(candidates) < 150:
            break

    if not best or best_score < 0.58:
        logger.info(f"FootyStats match non trouve: {home_team} vs {away_team}")
        return {}

    match_id = best.get("id") or best.get("match_id")
    details = get_match_details(match_id) if match_id else None
    enrichment = _parse_match_details(details or best, fallback=best)
    enrichment["match_score"] = round(best_score, 3)
    return enrichment if enrichment.get("status") == "ok" else {}
