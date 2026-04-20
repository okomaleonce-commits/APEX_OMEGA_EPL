import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY   = os.getenv("FOOTBALL_DATA_API_KEY", "")
DATA_DIR  = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

API_HOST = "v3.football.api-sports.io"
BASE_URL = f"https://{API_HOST}"
HEADERS  = {
    "x-rapidapi-key":  API_KEY,
    "x-rapidapi-host": API_HOST,
}

EPL_LEAGUE_ID = int(os.getenv("EPL_LEAGUE_ID", "39"))
EPL_SEASON    = int(os.getenv("EPL_SEASON", "2025"))
CACHE_TTL_H   = 6


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _is_fresh(path: Path, ttl_hours: int = CACHE_TTL_H) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def _load(path: Path) -> list:
    with open(path) as f:
        return json.load(f)


def _save(path: Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Fixtures à venir ──────────────────────────────────────────────

def fetch_upcoming(days_ahead: int = 3) -> list[dict]:
    """Retourne les fixtures EPL des prochains jours."""
    cache = _cache_path("fixtures_upcoming")

    if _is_fresh(cache):
        logger.debug("Fixtures depuis cache")
        return _load(cache)

    today = datetime.now(timezone.utc).date()
    end   = today + timedelta(days=days_ahead)

    try:
        resp = requests.get(
            f"{BASE_URL}/fixtures",
            headers=HEADERS,
            params={
                "league": EPL_LEAGUE_ID,
                "season": EPL_SEASON,
                "from":   str(today),
                "to":     str(end),
                "status": "NS",
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw      = resp.json().get("response", [])
        fixtures = [_parse(f) for f in raw]
        _save(cache, fixtures)
        logger.info(f"{len(fixtures)} fixtures EPL récupérés")
        return fixtures

    except Exception as e:
        logger.error(f"Erreur fetch fixtures: {e}")
        return _load(cache) if cache.exists() else []


def _parse(raw: dict) -> dict:
    fix   = raw["fixture"]
    home  = raw["teams"]["home"]
    away  = raw["teams"]["away"]
    venue = fix.get("venue", {})

    return {
        "fixture_id":       fix["id"],
        "kickoff_utc":      fix["date"],
        "round":            raw["league"].get("round", ""),
        "home_team":        home["name"],
        "home_team_id":     home["id"],
        "away_team":        away["name"],
        "away_team_id":     away["id"],
        "venue_name":       venue.get("name", ""),
        "venue_capacity":   venue.get("capacity", 0),
        "status":           fix["status"]["short"],
    }


# ── H2H ───────────────────────────────────────────────────────────

def fetch_h2h(home_id: int, away_id: int, n: int = 10) -> list[dict]:
    """Récupère les n derniers H2H entre deux équipes."""
    cache = _cache_path(f"h2h_{home_id}_{away_id}")

    if _is_fresh(cache, ttl_hours=48):
        return _load(cache)

    try:
        resp = requests.get(
            f"{BASE_URL}/fixtures/headtohead",
            headers=HEADERS,
            params={"h2h": f"{home_id}-{away_id}", "last": n},
            timeout=10,
        )
        resp.raise_for_status()
        results = []
        for r in resp.json().get("response", []):
            hg = r["goals"]["home"]
            ag = r["goals"]["away"]
            if hg is None or ag is None:
                continue
            results.append({
                "date":        r["fixture"]["date"][:10],
                "home_team":   r["teams"]["home"]["name"],
                "away_team":   r["teams"]["away"]["name"],
                "home_goals":  hg,
                "away_goals":  ag,
                "total_goals": hg + ag,
            })
        _save(cache, results)
        return results

    except Exception as e:
        logger.error(f"Erreur H2H: {e}")
        return _load(cache) if cache.exists() else []


# ── Stats équipe ──────────────────────────────────────────────────

def fetch_team_stats(team_id: int) -> dict:
    """Statistiques de la saison pour une équipe."""
    cache = _cache_path(f"stats_{team_id}")

    if _is_fresh(cache, ttl_hours=12):
        return _load(cache)

    try:
        resp = requests.get(
            f"{BASE_URL}/teams/statistics",
            headers=HEADERS,
            params={
                "league": EPL_LEAGUE_ID,
                "season": EPL_SEASON,
                "team":   team_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("response", {})
        stats = _parse_stats(data)
        _save(cache, stats)
        return stats

    except Exception as e:
        logger.error(f"Erreur stats équipe {team_id}: {e}")
        return _load(cache) if cache.exists() else {}


def _parse_stats(data: dict) -> dict:
    if not data:
        return {}

    goals   = data.get("goals", {})
    played  = data.get("fixtures", {}).get("played", {})
    total   = played.get("total", 1) or 1

    gf_total = goals.get("for",     {}).get("total", {}).get("total", 0) or 0
    ga_total = goals.get("against", {}).get("total", {}).get("total", 0) or 0

    form_str = data.get("form", "") or ""

    return {
        "team_name":          data.get("team", {}).get("name", ""),
        "team_id":            data.get("team", {}).get("id", 0),
        "matches_played":     total,
        "goals_scored_avg":   round(gf_total / total, 3),
        "goals_conceded_avg": round(ga_total / total, 3),
        "form_string":        form_str,
        "form_5":             list(form_str[-5:]) if form_str else [],
        "wins":    data.get("fixtures", {}).get("wins",   {}).get("total", 0),
        "draws":   data.get("fixtures", {}).get("draws",  {}).get("total", 0),
        "losses":  data.get("fixtures", {}).get("losses", {}).get("total", 0),
    }


# ── Blessés / Suspendus ───────────────────────────────────────────

def fetch_injuries(team_id: int) -> list[dict]:
    """Liste des blessés et suspendus."""
    cache = _cache_path(f"injuries_{team_id}")

    if _is_fresh(cache, ttl_hours=6):
        return _load(cache)

    try:
        resp = requests.get(
            f"{BASE_URL}/injuries",
            headers=HEADERS,
            params={
                "league": EPL_LEAGUE_ID,
                "season": EPL_SEASON,
                "team":   team_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        players = []
        for item in resp.json().get("response", []):
            p = item.get("player", {})
            players.append({
                "name":         p.get("name", ""),
                "position":     p.get("position", ""),
                "injury_type":  item.get("type", ""),
                "reason":       item.get("reason", ""),
            })
        _save(cache, players)
        return players

    except Exception as e:
        logger.error(f"Erreur injuries {team_id}: {e}")
        return _load(cache) if cache.exists() else []
