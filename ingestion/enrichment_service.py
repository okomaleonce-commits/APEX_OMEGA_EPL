import os
import logging

logger = logging.getLogger(__name__)

ENABLE_FOOTYSTATS = os.getenv("ENABLE_FOOTYSTATS", "true").lower() in {"1", "true", "yes", "on"}
ENABLE_ODDS_API_IO = os.getenv("ENABLE_ODDS_API_IO", "true").lower() in {"1", "true", "yes", "on"}


def _blend_probability(model_value, external_value, external_weight=0.35):
    if not external_value or external_value <= 0:
        return model_value
    external_value = max(0.01, min(float(external_value), 0.99))
    return round((model_value * (1 - external_weight)) + (external_value * external_weight), 4)


def _join_sources(*sources):
    out = []
    for src in sources:
        if not src:
            continue
        if isinstance(src, list):
            for item in src:
                if item and item not in out:
                    out.append(item)
        elif src not in out:
            out.append(src)
    return "+".join(out) if out else "reference"


def enrich_match(home_name, away_name, kickoff, model, odds_1x2, odds_ou25):
    odds_btts = {}
    sources = []
    dcs_bonus = 0

    if ENABLE_ODDS_API_IO:
        try:
            from ingestion.odds_api_io_service import get_best_odds_for_match
            odds_api_data = get_best_odds_for_match(home_name, away_name, kickoff_utc=kickoff)
            if odds_api_data:
                odds_1x2 = odds_api_data.get("odds_1x2") or odds_1x2
                odds_ou25 = odds_api_data.get("odds_ou25") or odds_ou25
                odds_btts = odds_api_data.get("odds_btts") or odds_btts
                sources.append("odds_api_io")
        except Exception as exc:
            logger.warning(f"Enrichment Odds-API.io skipped: {exc}")

    if ENABLE_FOOTYSTATS:
        try:
            from ingestion.footystats_service import find_footystats_match
            footy = find_footystats_match(home_name, away_name, kickoff_utc=kickoff)
            if footy:
                sources.append("footystats")
                odds_ou25 = odds_ou25 or footy.get("odds_ou25") or {}
                odds_btts = odds_btts or footy.get("odds_btts") or {}
                if odds_1x2.get("source") == "reference_model" and footy.get("odds_1x2"):
                    odds_1x2 = footy["odds_1x2"]
                if footy.get("over25_prob"):
                    model["over25"] = _blend_probability(model["over25"], footy["over25_prob"])
                    model["under25"] = round(1 - model["over25"], 4)
                if footy.get("btts_prob"):
                    model["btts_yes"] = _blend_probability(model["btts_yes"], footy["btts_prob"])
                    model["btts_no"] = round(1 - model["btts_yes"], 4)
                dcs_bonus += 5
        except Exception as exc:
            logger.warning(f"Enrichment FootyStats skipped: {exc}")

    odds_source = _join_sources(odds_1x2.get("source"), odds_ou25.get("source"), odds_btts.get("source"))
    enrichment_source = _join_sources(sources)

    return {
        "model": model,
        "odds_1x2": odds_1x2,
        "odds_ou25": odds_ou25,
        "odds_btts": odds_btts,
        "odds_source": odds_source,
        "enrichment_source": enrichment_source,
        "dcs_bonus": dcs_bonus,
    }
