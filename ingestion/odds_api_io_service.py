import os
import json
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

ODDS_API_IO_KEY = (
    os.getenv("ODDS_API_IO_KEY", "")
    or os.getenv("ODDS_API_KEY_IO", "")
    or os.getenv("ODDS_API_KEY", "")
)
ODDS_API_IO_BASE_URL = os.getenv("ODDS_API_IO_BASE_URL", "https://api.odds-api.io/v3").rstrip("/")
ODDS_API_BOOKMAKERS = [
    b.strip() for b in os.getenv("ODDS_API_BOOKMAKERS", "Bet365,Pinnacle,Unibet").split(",") if b.strip()
]
ODDS_API_LEAGUE_SLUG = os.getenv("ODDS_API_LEAGUE_SLUG", "england-premier-league")

DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", "./data"))
CACHE_DIR = DATA_DIR / "cache" / "odds_api_io"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {"Accept": "application/json"}


def _cache_path(name):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(name))
    return CACHE_DIR / f"{safe}.json"


def _is_fresh(path, ttl_minutes=10):
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
        logger.warning(f"Odds-API.io cache save failed: {exc}")


def _safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _api_call(endpoint, params=None, ttl_minutes=10, cache_key=None):
    if not ODDS_API_IO_KEY:
        return None
    params = dict(params or {})
    params["apiKey"] = ODDS_API_IO_KEY
    endpoint = endpoint.strip("/")
    cache = _cache_path(cache_key or f"{endpoint}_{json.dumps(params, sort_keys=True)}")
    if _is_fresh(cache, ttl_minutes=ttl_minutes):
        cached = _load(cache)
        if cached is not None:
            return cached
    try:
        resp = requests.get(
            f"{ODDS_API_IO_BASE_URL}/{endpoint}",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _save(cache, data)
        return data
    except Exception as exc:
        logger.warning(f"Odds-API.io call failed [{endpoint}]: {exc}")
        return _load(cache)


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


def _match_score(home, away, event):
    ev_home = event.get("home", "") or event.get("home_team", "") or event.get("homeName", "")
    ev_away = event.get("away", "") or event.get("away_team", "") or event.get("awayName", "")
    direct = (_name_score(home, ev_home) + _name_score(away, ev_away)) / 2
    swapped = (_name_score(home, ev_away) + _name_score(away, ev_home)) / 2
    return max(direct, swapped)


def _window_from_kickoff(kickoff_utc):
    if not kickoff_utc:
        now = datetime.now(timezone.utc)
        return now.isoformat().replace("+00:00", "Z"), (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    try:
        dt = datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
    except Exception:
        now = datetime.now(timezone.utc)
        return now.isoformat().replace("+00:00", "Z"), (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    start = dt - timedelta(hours=24)
    end = dt + timedelta(hours=24)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def get_events(status="pending,live", league_slug=None, date_from=None, date_to=None):
    params = {"sport": "football", "status": status}
    if league_slug:
        params["league"] = league_slug
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    return _api_call("events", params=params, ttl_minutes=10, cache_key=f"events_{league_slug}_{date_from}_{date_to}_{status}")


def find_event(home_team, away_team, kickoff_utc=None):
    if not ODDS_API_IO_KEY:
        return None
    date_from, date_to = _window_from_kickoff(kickoff_utc)
    events = get_events(
        status="pending,live",
        league_slug=ODDS_API_LEAGUE_SLUG,
        date_from=date_from,
        date_to=date_to,
    )
    if isinstance(events, dict):
        events = events.get("data") or events.get("events") or events.get("response") or []
    if not isinstance(events, list):
        return None
    best = None
    best_score = 0.0
    for event in events:
        if not isinstance(event, dict):
            continue
        score = _match_score(home_team, away_team, event)
        if score > best_score:
            best = event
            best_score = score
    if best and best_score >= 0.58:
        best["match_score"] = round(best_score, 3)
        return best
    return None


def get_odds_for_event(event_id, bookmakers=None):
    if not event_id:
        return None
    books = bookmakers or ODDS_API_BOOKMAKERS
    return _api_call(
        "odds",
        params={"eventId": event_id, "bookmakers": ",".join(books[:30])},
        ttl_minutes=5,
        cache_key=f"odds_{event_id}_{'_'.join(books[:30])}",
    )


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


def _demarginize_ou(over, under, source):
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


def _update_best(best, key, odd, bookmaker):
    odd = _safe_float(odd)
    if odd <= 1.01:
        return
    if odd > best.get(key, {}).get("odd", 0):
        best[key] = {"odd": odd, "bookmaker": bookmaker}


def _iter_bookmakers(payload):
    if isinstance(payload, dict):
        bookmakers = payload.get("bookmakers") or payload.get("data") or payload.get("odds") or {}
    else:
        bookmakers = payload or {}
    if isinstance(bookmakers, dict):
        for bookmaker, markets in bookmakers.items():
            yield bookmaker, markets
    elif isinstance(bookmakers, list):
        for item in bookmakers:
            if isinstance(item, dict):
                yield item.get("bookmaker") or item.get("name") or "unknown", item.get("markets") or item.get("odds") or item.get("bets") or []


def parse_best_prices(odds_payload):
    if not odds_payload:
        return {}
    best = {}
    for bookmaker, markets in _iter_bookmakers(odds_payload):
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            name = str(market.get("name") or market.get("key") or market.get("market") or "").lower()
            rows = market.get("odds") or market.get("outcomes") or market.get("values") or []
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                label = str(row.get("value") or row.get("name") or row.get("label") or "").lower()
                odd = row.get("odd") or row.get("price") or row.get("odds")
                if name in {"ml", "moneyline", "match winner", "1x2"}:
                    _update_best(best, "home", row.get("home") or (odd if label in {"home", "1"} else None), bookmaker)
                    _update_best(best, "draw", row.get("draw") or (odd if label in {"draw", "x"} else None), bookmaker)
                    _update_best(best, "away", row.get("away") or (odd if label in {"away", "2"} else None), bookmaker)
                elif name in {"totals", "goals over/under", "total goals"} or "over/under" in name:
                    hdp = _safe_float(row.get("hdp") or row.get("line") or row.get("point"), -999)
                    if abs(hdp - 2.5) < 0.01 or "2.5" in label:
                        _update_best(best, "over25", row.get("over") or (odd if "over" in label else None), bookmaker)
                        _update_best(best, "under25", row.get("under") or (odd if "under" in label else None), bookmaker)
                elif ("both" in name and "score" in name) or "btts" in name:
                    _update_best(best, "btts_yes", row.get("yes") or (odd if label in {"yes", "oui"} else None), bookmaker)
                    _update_best(best, "btts_no", row.get("no") or (odd if label in {"no", "non"} else None), bookmaker)

    odds_1x2 = _demarginize_1x2(
        best.get("home", {}).get("odd"),
        best.get("draw", {}).get("odd"),
        best.get("away", {}).get("odd"),
        "odds_api_io",
    )
    odds_ou25 = _demarginize_ou(
        best.get("over25", {}).get("odd"),
        best.get("under25", {}).get("odd"),
        "odds_api_io",
    )
    odds_btts = _demarginize_btts(
        best.get("btts_yes", {}).get("odd"),
        best.get("btts_no", {}).get("odd"),
        "odds_api_io",
    )
    return {
        "source": "odds_api_io",
        "event_id": odds_payload.get("id") if isinstance(odds_payload, dict) else None,
        "best_bookmakers": {k: v.get("bookmaker") for k, v in best.items()},
        "odds_1x2": odds_1x2,
        "odds_ou25": odds_ou25,
        "odds_btts": odds_btts,
    }


def get_best_odds_for_match(home_team, away_team, kickoff_utc=None, bookmakers=None):
    event = find_event(home_team, away_team, kickoff_utc=kickoff_utc)
    if not event:
        return {}
    event_id = event.get("id") or event.get("eventId") or event.get("event_id")
    odds_payload = get_odds_for_event(event_id, bookmakers=bookmakers)
    parsed = parse_best_prices(odds_payload)
    if parsed:
        parsed["event"] = {
            "id": event_id,
            "home": event.get("home") or event.get("home_team"),
            "away": event.get("away") or event.get("away_team"),
            "date": event.get("date") or event.get("commence_time"),
            "match_score": event.get("match_score"),
        }
    return parsed


def get_value_bets(bookmaker="Bet365", sport="football", league_slug=None, include_event_details=True):
    params = {
        "bookmaker": bookmaker,
        "sport": sport,
        "includeEventDetails": str(include_event_details).lower(),
    }
    if league_slug:
        params["league"] = league_slug
    return _api_call("value-bets", params=params, ttl_minutes=10)


def get_arbitrage_bets(bookmakers=None, limit=50, include_event_details=True):
    books = bookmakers or ODDS_API_BOOKMAKERS
    params = {
        "bookmakers": ",".join(books[:30]),
        "limit": limit,
        "includeEventDetails": str(include_event_details).lower(),
    }
    return _api_call("arbitrage-bets", params=params, ttl_minutes=5)
