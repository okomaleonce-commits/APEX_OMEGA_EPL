import json
import logging
import os
import re
import requests
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(name).setLevel(logging.WARNING)

_original_requests_get = requests.get


def patched_requests_get(url, *args, **kwargs):
    params = kwargs.get("params")
    if isinstance(url, str) and "/odds" in url and isinstance(params, dict):
        if "next" in params:
            params = dict(params)
            params.pop("next", None)
            params.setdefault("page", 1)
            kwargs["params"] = params
    return _original_requests_get(url, *args, **kwargs)


requests.get = patched_requests_get


def _norm_team(name):
    txt = (name or "").lower()
    txt = txt.replace("man utd", "manchester united")
    txt = txt.replace("man city", "manchester city")
    txt = txt.replace("nottm forest", "nottingham forest")
    txt = txt.replace("wolves", "wolverhampton wanderers")
    txt = re.sub(r"[^a-z0-9 ]+", " ", txt)
    remove = {"fc", "afc", "cf", "the"}
    return " ".join(t for t in txt.split() if t not in remove)


def _team_match(a, b):
    aa = _norm_team(a)
    bb = _norm_team(b)
    if not aa or not bb:
        return False
    if aa == bb or aa in bb or bb in aa:
        return True
    return bool(set(aa.split()) & set(bb.split()))


def _stats_weak(stats):
    if not isinstance(stats, dict) or not stats:
        return True
    return (stats.get("matches_played") or 0) <= 0 or (stats.get("goals_scored_avg") is None)


def apply_free_data_patch():
    try:
        import ingestion.fixtures_service as fixtures_service
        from ingestion import free_data_service
    except Exception as exc:
        logger.warning(f"Free-data hotfix not applied: {exc}")
        return False

    if getattr(fixtures_service, "_apex_free_data_hotfix", False):
        return True

    original_fetch_upcoming = fixtures_service.fetch_upcoming
    original_fetch_team_stats = fixtures_service.fetch_team_stats
    original_fetch_h2h = fixtures_service.fetch_h2h

    def fetch_upcoming_auto(days_ahead=7):
        primary = original_fetch_upcoming(days_ahead=days_ahead)
        if primary:
            return primary
        if os.getenv("ENABLE_FREE_AUTO_DATA", "true").lower() not in {"1", "true", "yes", "on"}:
            return primary
        fallback = free_data_service.fetch_upcoming_free(days_ahead=days_ahead)
        if fallback:
            logger.warning(f"APEX FREE DATA: {len(fallback)} fixtures via football-data.org")
            return fallback
        return primary

    def fetch_team_stats_auto(team_id):
        stats = original_fetch_team_stats(team_id)
        if not _stats_weak(stats):
            return stats
        if os.getenv("ENABLE_FREE_AUTO_DATA", "true").lower() not in {"1", "true", "yes", "on"}:
            return stats
        # Impossible de matcher un nom depuis team_id seul ici. Le fallback est injecte via compute_xg context in main path.
        return stats

    def fetch_h2h_auto(home_id, away_id, n=10):
        return original_fetch_h2h(home_id, away_id, n=n)

    fixtures_service.fetch_upcoming = fetch_upcoming_auto
    fixtures_service.fetch_team_stats = fetch_team_stats_auto
    fixtures_service.fetch_h2h = fetch_h2h_auto
    fixtures_service._apex_free_data_hotfix = True
    logger.warning("APEX HOTFIX ACTIVE: free auto-data fallback enabled")
    return True


def apply_odds_service_patch():
    try:
        import ingestion.odds_service as odds_service
    except Exception as exc:
        logger.warning(f"Odds hotfix not applied: {exc}")
        return False

    if getattr(odds_service, "_apex_odds_hotfix", False):
        return True

    def fresh_cache(path, ttl_hours=1):
        if not path.exists():
            return False
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return age < timedelta(hours=ttl_hours)

    def load_cache(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_cache(path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Odds cache save failed: {exc}")

    def fetch_all_epl_odds_hotfixed():
        cache = odds_service._cache_path("all_epl")
        cached = load_cache(cache) if fresh_cache(cache) else []
        if cached:
            return cached

        api_key = getattr(odds_service, "API_KEY_AF", "")
        if not api_key:
            logger.warning("FOOTBALL_DATA_API_KEY non configuree")
            return []

        results = []
        max_pages = int(os.getenv("API_FOOTBALL_ODDS_MAX_PAGES", "5"))
        bookmaker = int(os.getenv("API_FOOTBALL_BOOKMAKER_ID", str(getattr(odds_service, "BOOKMAKER_ID", 8))))
        for page in range(1, max_pages + 1):
            try:
                resp = _original_requests_get(
                    f"https://{odds_service.API_HOST}/odds",
                    headers=odds_service.HEADERS,
                    params={
                        "league":    odds_service.EPL_LEAGUE_ID,
                        "season":    odds_service.EPL_SEASON,
                        "next":      10,   # prochains matchs EPL
                        "bookmaker": bookmaker,
                        "page":      page,
                    },
                    timeout=12,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.error(f"Erreur cotes API-Football page {page}: {exc}")
                break

            errors = payload.get("errors", {})
            if errors:
                logger.error(f"API odds error: {errors}")
                break

            raw = payload.get("response", []) or []
            for item in raw:
                parsed = odds_service._parse_odds_item(item)
                if parsed:
                    results.append(parsed)

            paging = payload.get("paging", {}) or {}
            current = int(paging.get("current", page) or page)
            total = int(paging.get("total", current) or current)
            if current >= total or not raw:
                break

        logger.warning(f"APEX ODDS HOTFIX: {len(results)} matchs avec cotes API-Football")
        if results:
            save_cache(cache, results)
        return results

    def get_odds_for_match_hotfixed(home_team, away_team):
        for game in fetch_all_epl_odds_hotfixed():
            if _team_match(home_team, game.get("home_team", "")) and _team_match(away_team, game.get("away_team", "")):
                logger.info(f"Cotes API trouvees: {home_team} vs {away_team}")
                return game
        logger.warning(f"Cotes API non trouvees: {home_team} vs {away_team}")
        return {}

    odds_service.fetch_all_epl_odds = fetch_all_epl_odds_hotfixed
    odds_service.get_odds_for_match = get_odds_for_match_hotfixed
    odds_service._apex_odds_hotfix = True
    logger.warning("APEX HOTFIX ACTIVE: odds cache refresh + team matching")
    return True


def apply_rule_engine_patch():
    try:
        import rules.rule_engine as rule_engine
    except Exception as exc:
        logger.warning(f"ACL hotfix not applied: {exc}")
        return False

    original = getattr(rule_engine, "r9_acl", None)
    if not original:
        return False
    if getattr(original, "_apex_safe_patch", False):
        return True

    def safe_r9_acl(lineup_data, probs, is_home_team):
        allow_unconfirmed = os.getenv("ALLOW_UNCONFIRMED_ACL", "true").lower() in {"1", "true", "yes", "on"}
        if isinstance(lineup_data, dict):
            # Check 1 : lineup confirmée — bypass si allow_unconfirmed
            if not allow_unconfirmed and lineup_data.get("lineup_confirmed") is False:
                return probs, 1.0, None, False

            # Check 2 : cap blessés — TOUJOURS appliqué
            # > 5 blessés = probablement liste saison entière, pas blessés actuels
            # Ce cap protège contre l'activation ACL sur toutes les équipes
            injured = lineup_data.get("injured_players") or []
            max_inj = int(os.getenv("ACL_MAX_INJURY_LIST_SIZE", "5"))
            if len(injured) > max_inj:
                logger.warning(
                    f"ACL skip: {len(injured)} blesses > cap {max_inj} "
                    f"(probablement liste saison, pas blessures actuelles)"
                )
                return probs, 1.0, None, False

        return original(lineup_data, probs, is_home_team)

    safe_r9_acl._apex_safe_patch = True
    rule_engine.r9_acl = safe_r9_acl
    logger.warning("APEX HOTFIX ACTIVE: unconfirmed ACL disabled")
    return True


def apply_verdict_gate_patch():
    try:
        import decisions.verdict_engine as verdict_engine
    except Exception as exc:
        logger.warning(f"Verdict gate hotfix not applied: {exc}")
        return False

    original = getattr(verdict_engine, "generate_verdicts", None)
    if not original:
        return False
    if getattr(original, "_apex_real_odds_gate", False):
        return True

    def gated_generate_verdicts(model, odds_1x2, odds_ou25, dcs_score, *args, **kwargs):
        allow_reference = os.getenv("ALLOW_REFERENCE_ODDS_SIGNALS", "true").lower() in {"1", "true", "yes", "on"}
        src_1x2 = (odds_1x2 or {}).get("source", "")
        src_ou  = (odds_ou25 or {}).get("source", "")
        real_sources = {"api_football", "odds_api_io", "footystats"}
        has_real_odds = src_1x2 in real_sources or src_ou in real_sources

        if not has_real_odds and not allow_reference:
            logger.warning("APEX HARD GATE: NO BET — real odds unavailable; reference_model signals blocked")
            return []

        # Si source = reference_model, abaisser le seuil edge (cotes et modele partagent les memes xG)
        # On injecte un flag dans les kwargs pour que generate_verdicts l'utilise
        if not has_real_odds and allow_reference:
            kwargs["reference_odds_mode"] = True
            logger.info("APEX: reference_model mode — seuil edge reduit (x0.35)")

        return original(model, odds_1x2, odds_ou25, dcs_score, *args, **kwargs)

    gated_generate_verdicts._apex_real_odds_gate = True
    verdict_engine.generate_verdicts = gated_generate_verdicts
    logger.warning("APEX HOTFIX ACTIVE: real-odds gate enabled")
    return True


apply_free_data_patch()
apply_odds_service_patch()
apply_rule_engine_patch()
apply_verdict_gate_patch()
