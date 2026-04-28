"""
FootyStats API — Source P1 pour xG EPL.
Fournit :
  - xG for/against par équipe (season)
  - xG match par match (pour calcul CCR ratio)
  - BTTS%, Over/Under% par équipe
  - H2H stats enrichies

API doc : https://footystats.org/api/documentations
Plan gratuit : 100 req/jour · Plan payant : illimité
"""
import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FOOTYSTATS_KEY = os.getenv("FOOTYSTATS_KEY", "")
DATA_DIR  = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL      = "https://api.football-data-api.com"
# FootyStats season IDs — EPL: 2012 (2024/25), will auto-discover 2025/26
# If 2012 fails, the service tries to find the correct season dynamically
EPL_SEASON_ID = int(os.getenv("FOOTYSTATS_EPL_SEASON_ID", "10771"))  # 2025/26 probable ID


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"fs_{name}.json"


def _is_fresh(path: Path, ttl_hours: float = 12) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def _get(endpoint: str, params: dict) -> dict:
    """Appel API FootyStats avec gestion d'erreurs."""
    if not FOOTYSTATS_KEY:
        return {}
    try:
        resp = requests.get(
            f"{BASE_URL}/{endpoint}",
            params={"key": FOOTYSTATS_KEY, **params},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"FootyStats {endpoint}: {e}")
        return {}


# ── Stats équipe ──────────────────────────────────────────────────

def get_team_stats(team_name: str) -> dict:
    """
    Retourne les stats xG saison d'une équipe via FootyStats.
    Utilisé à la place de API-Football pour les attack_rate/defense_rate.

    Retourne :
    {
        xg_for_avg, xg_against_avg,
        attack_rate, defense_rate,
        btts_pct, over25_pct,
        goals_scored_avg, goals_conceded_avg,
        matches_played, source
    }
    """
    safe_name = team_name.lower().replace(" ", "_").replace(".", "")
    cache = _cache_path(f"team_{safe_name}")

    if _is_fresh(cache, ttl_hours=12):
        data = json.loads(cache.read_text())
        if isinstance(data, dict) and data:
            return data

    sid  = discover_epl_season_id()
    data = _get("league-teams", {
        "season_id": sid,
        "include":   "stats",
    })

    teams = data.get("data", [])
    if not teams:
        logger.warning(f"FootyStats: aucune équipe retournée (clé invalide ou plan?)")
        return {}

    # Chercher l'équipe par nom
    for team in teams:
        name = team.get("cleanName", "") or team.get("name", "")
        if _fuzzy_match(team_name, name):
            stats = team.get("stats", {})
            result = _parse_team_stats(team, stats)
            cache.write_text(json.dumps(result, indent=2))
            logger.info(f"FootyStats: stats {team_name} OK (xG for={result.get('xg_for_avg')})")
            return result

    logger.warning(f"FootyStats: équipe '{team_name}' non trouvée")
    return {}


def _parse_team_stats(team: dict, stats: dict) -> dict:
    """Parse les stats FootyStats en format APEX-ENGINE."""
    played = stats.get("seasonMatchesPlayed_overall", 1) or 1

    # xG
    xg_for  = stats.get("seasonXGFor_overall",     0) or 0
    xg_ag   = stats.get("seasonXGAgainst_overall", 0) or 0

    # Goals (fallback si xG absent)
    gf = stats.get("seasonGoals_overall",         0) or 0
    ga = stats.get("seasonConceded_overall",       0) or 0

    # BTTS / Over
    btts_pct  = stats.get("seasonBTTSPercentage_overall",  None)
    over25_pct = stats.get("seasonOver25Percentage_overall", None)

    xg_for_avg = round(xg_for / played, 3) if xg_for else round(gf / played, 3)
    xg_ag_avg  = round(xg_ag  / played, 3) if xg_ag  else round(ga / played, 3)
    avg_team   = 1.445

    return {
        "xg_for_avg":          xg_for_avg,
        "xg_against_avg":      xg_ag_avg,
        "attack_rate":         round(xg_for_avg / avg_team, 4),
        "defense_rate":        round(xg_ag_avg  / avg_team, 4),
        "goals_scored_avg":    round(gf / played, 3),
        "goals_conceded_avg":  round(ga / played, 3),
        "btts_pct":            (btts_pct  / 100) if btts_pct  is not None else 0.548,
        "over25_pct":          (over25_pct / 100) if over25_pct is not None else 0.613,
        "matches_played":      played,
        "source":              "footystats",
        "dcs_contribution":    20,   # source P1 → +20 pts DCS
    }


# ── xG match par match (pour CCR) ────────────────────────────────

def get_team_xg_matchlog(team_name: str, n: int = 6) -> list[dict]:
    """
    Retourne le log xG des n derniers matchs d'une équipe.
    Utilisé pour calculer le ratio CCR (goals_réels / xG_cumulé).

    Retourne liste de :
    {date, xg_for, xg_against, goals_for, goals_against, home_away}
    """
    safe_name = team_name.lower().replace(" ", "_").replace(".", "")
    cache = _cache_path(f"matchlog_{safe_name}")

    if _is_fresh(cache, ttl_hours=6):
        data = json.loads(cache.read_text())
        if isinstance(data, list):
            return data

    sid  = discover_epl_season_id()
    data = _get("league-matches", {
        "season_id": sid,
        "team_name": team_name,
        "status":    "complete",
    })

    matches_raw = data.get("data", [])
    if not matches_raw:
        return []

    # Trier par date décroissante et prendre les n derniers
    matches_raw = sorted(
        matches_raw,
        key=lambda m: m.get("date_unix", 0),
        reverse=True
    )[:n]

    logs = []
    for m in matches_raw:
        home_name = m.get("home_name", "")
        is_home   = _fuzzy_match(team_name, home_name)

        if is_home:
            xg_for  = m.get("home_xg",    0) or 0
            xg_ag   = m.get("away_xg",    0) or 0
            gf      = m.get("homeGoalCount", 0) or 0
            ga      = m.get("awayGoalCount", 0) or 0
        else:
            xg_for  = m.get("away_xg",    0) or 0
            xg_ag   = m.get("home_xg",    0) or 0
            gf      = m.get("awayGoalCount", 0) or 0
            ga      = m.get("homeGoalCount", 0) or 0

        logs.append({
            "date":        m.get("date_unix", 0),
            "xg_for":      round(float(xg_for), 3),
            "xg_against":  round(float(xg_ag),  3),
            "goals_for":   int(gf),
            "goals_against": int(ga),
            "home_away":   "home" if is_home else "away",
        })

    cache.write_text(json.dumps(logs, indent=2))
    return logs


def calculate_ccr(team_name: str, n: int = 5) -> dict:
    """
    Calcule le ratio CCR (Conversion Crisis Reversal).
    ratio = goals_réels / xG_cumulé sur les n derniers matchs.

    Retourne :
    {
        ratio, goals_real, xg_cumulated,
        matches_analyzed, ccr_active, flag, source
    }
    """
    logs = get_team_xg_matchlog(team_name, n=n)

    if not logs:
        return {
            "ratio":            1.0,
            "goals_real":       None,
            "xg_cumulated":     None,
            "matches_analyzed": 0,
            "ccr_active":       False,
            "flag":             None,
            "source":           "footystats_unavailable",
        }

    goals_total = sum(m["goals_for"] for m in logs)
    xg_total    = sum(m["xg_for"]    for m in logs)

    if xg_total <= 0:
        ratio = 1.0
    else:
        ratio = round(goals_total / xg_total, 3)

    n_matches = len(logs)

    # Appliquer les seuils CCR (Règle R7)
    if n_matches >= 4 and ratio < 0.50:
        flag = "SNAP_ATTENDU"
        active = True
    elif n_matches >= 4 and ratio < 0.70:
        flag = "REGRESSION_IMMINENTE"
        active = True
    else:
        flag   = None
        active = False

    return {
        "ratio":            ratio,
        "goals_real":       goals_total,
        "xg_cumulated":     round(xg_total, 3),
        "matches_analyzed": n_matches,
        "ccr_active":       active,
        "flag":             flag,
        "source":           "footystats",
    }


# ── Disponibilité API ─────────────────────────────────────────────

def test_footystats() -> dict:
    """Teste la clé FootyStats — essaie plusieurs endpoints pour diagnostic."""
    if not FOOTYSTATS_KEY:
        return {"status": "NOT_CONFIGURED", "detail": "FOOTYSTATS_KEY non définie"}

    # Test 1 : endpoint /league-list (ne nécessite pas de season_id)
    data = _get("league-list", {"country_id": 2})  # 2 = England
    if data and "data" in data:
        leagues = data["data"]
        epl = next((l for l in leagues if "premier" in l.get("name", "").lower()), None)
        if epl:
            season_raw = epl.get("season", {})
            if isinstance(season_raw, list):
                season_raw = season_raw[-1] if season_raw else {}
            season_id = (season_raw.get("id") or season_raw.get("season_id") or EPL_SEASON_ID) if isinstance(season_raw, dict) else EPL_SEASON_ID
            return {
                "status": "OK",
                "detail": f"FootyStats OK | EPL season_id={season_id}",
                "season_id": season_id,
            }
        return {"status": "OK", "detail": f"FootyStats OK | {len(leagues)} ligues trouvées"}

    # Test 2 : endpoint /today-matches (basique)
    data2 = _get("today-matches", {})
    if data2 and not data2.get("error"):
        return {"status": "OK", "detail": "FootyStats OK (today-matches)"}

    # Test 3 : vérifier le message d'erreur
    if data and data.get("error"):
        return {
            "status": "ERROR",
            "detail": f"FootyStats erreur: {data.get('error', 'unknown')}",
        }

    return {
        "status": "ERROR",
        "detail": "Clé invalide, quota dépassé, ou plan insuffisant",
    }


def discover_epl_season_id() -> int:
    """
    Découvre le season_id EPL courant via FootyStats.
    FootyStats retourne "season" comme une liste ou un dict selon le plan.
    """
    cache = _cache_path("epl_season_id")
    if _is_fresh(cache, ttl_hours=24):
        try:
            import json as _json
            data = _json.loads(cache.read_text())
            if isinstance(data, int) and data > 0:
                return data
        except Exception:
            pass

    data = _get("league-list", {"country_id": 2})
    if data and "data" in data:
        for league in data["data"]:
            name    = league.get("name", "").lower()
            country = league.get("country", "").lower()
            if "premier" not in name:
                continue

            # "season" peut être un dict OU une liste de dicts selon le plan
            season_raw = league.get("season", {})
            if isinstance(season_raw, list):
                # Prendre la saison la plus récente (dernière ou première)
                season_raw = season_raw[-1] if season_raw else {}
            if isinstance(season_raw, dict):
                sid = season_raw.get("id") or season_raw.get("season_id")
                if sid:
                    logger.info(f"FootyStats EPL season_id découvert: {sid}")
                    import json as _json
                    cache.write_text(_json.dumps(int(sid)))
                    return int(sid)

    logger.warning(f"FootyStats: season_id non trouvé, utilisation du fallback {EPL_SEASON_ID}")
    return EPL_SEASON_ID  # fallback


# ── Helpers ───────────────────────────────────────────────────────

def _fuzzy_match(query: str, candidate: str) -> bool:
    """Matching souple entre noms d'équipes."""
    q = query.lower().strip()
    c = candidate.lower().strip()
    if q == c:
        return True
    if q in c or c in q:
        return True
    # Mots communs (au moins 1 mot significatif en commun)
    q_words = {w for w in q.split() if len(w) > 3}
    c_words = {w for w in c.split() if len(w) > 3}
    return bool(q_words & c_words)
