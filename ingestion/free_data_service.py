import os
import json
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache" / "free_data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FOOTBALL_DATA_ORG_TOKEN = (
    os.getenv("FOOTBALL_DATA_ORG_TOKEN", "")
    or os.getenv("FD_ORG_TOKEN", "")
    or os.getenv("FOOTBALLDATA_TOKEN", "")
)
FOOTBALL_DATA_ORG_BASE = os.getenv("FOOTBALL_DATA_ORG_BASE", "https://api.football-data.org/v4").rstrip("/")
FOOTBALL_DATA_ORG_COMPETITION = os.getenv("FOOTBALL_DATA_ORG_COMPETITION", "PL")
FOOTBALL_DATA_ORG_SEASON = os.getenv("FOOTBALL_DATA_ORG_SEASON", "2025")
FREE_DATA_TTL_HOURS = int(os.getenv("FREE_DATA_TTL_HOURS", "6"))


def _cache_path(name):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(name))
    return CACHE_DIR / f"{safe}.json"


def _is_fresh(path, ttl_hours=FREE_DATA_TTL_HOURS):
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return age < timedelta(hours=ttl_hours)


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
        logger.warning(f"free-data cache save failed: {exc}")


def _headers():
    h = {"Accept": "application/json"}
    if FOOTBALL_DATA_ORG_TOKEN:
        h["X-Auth-Token"] = FOOTBALL_DATA_ORG_TOKEN
    return h


def _api_get(path, params=None, cache_key=None, ttl_hours=FREE_DATA_TTL_HOURS):
    cache = _cache_path(cache_key or path)
    if _is_fresh(cache, ttl_hours=ttl_hours):
        cached = _load(cache)
        if cached is not None:
            return cached

    if not FOOTBALL_DATA_ORG_TOKEN:
        logger.warning("football-data.org token missing: set FOOTBALL_DATA_ORG_TOKEN for free auto-data fallback")
        return _load(cache)

    try:
        resp = requests.get(
            f"{FOOTBALL_DATA_ORG_BASE}/{path.lstrip('/')}",
            headers=_headers(),
            params=params or {},
            timeout=15,
        )
        if resp.status_code in {401, 403, 429}:
            logger.warning(f"football-data.org blocked [{resp.status_code}] for {path}")
            return _load(cache)
        resp.raise_for_status()
        data = resp.json()
        _save(cache, data)
        return data
    except Exception as exc:
        logger.warning(f"football-data.org fetch failed [{path}]: {exc}")
        return _load(cache)


def _norm_team(name):
    text = (name or "").lower()
    replacements = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "man city": "manchester city",
        "nottm forest": "nottingham forest",
        "wolves": "wolverhampton wanderers",
        "spurs": "tottenham hotspur",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    for token in ["fc", "afc", "cf", "the"]:
        text = text.replace(f" {token} ", " ")
    return " ".join(text.replace("-", " ").replace(".", " ").split())


def _team_match(a, b):
    aa = _norm_team(a)
    bb = _norm_team(b)
    if not aa or not bb:
        return False
    if aa == bb or aa in bb or bb in aa:
        return True
    a_tokens = set(aa.split())
    b_tokens = set(bb.split())
    return bool(a_tokens & b_tokens)


def get_standings_table():
    data = _api_get(
        f"competitions/{FOOTBALL_DATA_ORG_COMPETITION}/standings",
        params={"season": FOOTBALL_DATA_ORG_SEASON},
        cache_key=f"standings_{FOOTBALL_DATA_ORG_COMPETITION}_{FOOTBALL_DATA_ORG_SEASON}",
        ttl_hours=6,
    )
    if not isinstance(data, dict):
        return []
    for standing in data.get("standings", []):
        if standing.get("type") == "TOTAL":
            return standing.get("table", []) or []
    return []


def find_team_standing(team_name):
    for row in get_standings_table():
        team = row.get("team", {})
        if _team_match(team_name, team.get("name", "")) or _team_match(team_name, team.get("shortName", "")):
            return row
    return {}


def get_matches(date_from=None, date_to=None, status=None):
    params = {"season": FOOTBALL_DATA_ORG_SEASON}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    if status:
        params["status"] = status
    data = _api_get(
        f"competitions/{FOOTBALL_DATA_ORG_COMPETITION}/matches",
        params=params,
        cache_key=f"matches_{FOOTBALL_DATA_ORG_COMPETITION}_{FOOTBALL_DATA_ORG_SEASON}_{date_from}_{date_to}_{status}",
        ttl_hours=6,
    )
    if isinstance(data, dict):
        return data.get("matches", []) or []
    return []


def fetch_upcoming_free(days_ahead=7):
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)
    rows = get_matches(str(today), str(end), status="SCHEDULED")
    fixtures = []
    for m in rows:
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        fixtures.append({
            "fixture_id": int(m.get("id") or 0),
            "kickoff_utc": m.get("utcDate", ""),
            "round": f"Matchday {m.get('matchday', '')}",
            "home_team": home.get("name", ""),
            "home_team_id": int(home.get("id") or 0),
            "away_team": away.get("name", ""),
            "away_team_id": int(away.get("id") or 0),
            "venue_name": "",
            "venue_capacity": 0,
            "status": m.get("status", "SCHEDULED"),
            "source": "football-data.org",
        })
    return [f for f in fixtures if f["home_team"] and f["away_team"]]


def fetch_team_stats_free(team_name):
    row = find_team_standing(team_name)
    if not row:
        return {}
    played = row.get("playedGames") or 0
    if not played:
        return {}
    gf = row.get("goalsFor") or 0
    ga = row.get("goalsAgainst") or 0
    return {
        "team_name": row.get("team", {}).get("name", team_name),
        "team_id": int(row.get("team", {}).get("id") or 0),
        "matches_played": played,
        "goals_scored_avg": round(gf / played, 3),
        "goals_conceded_avg": round(ga / played, 3),
        "form_string": "",
        "form_5": [],
        "wins": row.get("won") or 0,
        "draws": row.get("draw") or 0,
        "losses": row.get("lost") or 0,
        "points": row.get("points") or 0,
        "goal_difference": row.get("goalDifference") or 0,
        "source": "football-data.org",
    }


def fetch_h2h_free(home_name, away_name, lookback_days=900):
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    rows = get_matches(str(start), str(today), status="FINISHED")
    out = []
    for m in rows:
        home = m.get("homeTeam", {}).get("name", "")
        away = m.get("awayTeam", {}).get("name", "")
        if not ((_team_match(home_name, home) and _team_match(away_name, away)) or (_team_match(home_name, away) and _team_match(away_name, home))):
            continue
        score = m.get("score", {}).get("fullTime", {})
        hg = score.get("home")
        ag = score.get("away")
        if hg is None or ag is None:
            continue
        out.append({
            "date": (m.get("utcDate") or "")[:10],
            "home_team": home,
            "away_team": away,
            "home_goals": int(hg),
            "away_goals": int(ag),
            "total_goals": int(hg) + int(ag),
            "source": "football-data.org",
        })
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    return out[:10]


def get_free_data_pack(home_name, away_name):
    return {
        "home_stats": fetch_team_stats_free(home_name),
        "away_stats": fetch_team_stats_free(away_name),
        "h2h": fetch_h2h_free(home_name, away_name),
        "source": "football-data.org",
    }
