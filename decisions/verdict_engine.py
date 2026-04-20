import logging

logger = logging.getLogger(__name__)

EDGE_MIN = {
    "1x2_low":    0.04,
    "1x2_mid":    0.05,
    "1x2_high":   0.06,
    "1x2_xlarge": 0.09,
    "over25":     0.05,
    "under25":    0.07,
    "btts_yes":   0.05,
    "btts_no":    0.09,
}

KELLY = {
    "1x2_low":    0.20,
    "1x2_mid":    0.25,
    "1x2_high":   0.20,
    "1x2_xlarge": 0.125,
    "over25":     0.25,
    "under25":    0.20,
    "btts_yes":   0.25,
    "btts_no":    0.167,
}

# Labels lisibles pour Telegram
MARKET_LABELS = {
    "1x2_home": "Victoire Domicile",
    "1x2_draw": "Match Nul",
    "1x2_away": "Victoire Exterieur",
    "over25":   "Over 2.5 buts",
    "under25":  "Under 2.5 buts",
    "btts_yes": "Les deux equipes marquent (Oui)",
    "btts_no":  "Les deux equipes marquent (Non)",
}

MAX_STAKE = 0.05
DCS_GATE  = 70


def get_odds_category(odds):
    if odds <= 1.60: return "low"
    if odds <= 2.50: return "mid"
    if odds <= 4.00: return "high"
    return "xlarge"


def kelly_fraction(prob, odds, fraction):
    b = odds - 1
    if b <= 0: return 0.0
    raw = (b * prob - (1 - prob)) / b
    return max(0.0, round(raw * fraction, 4))


def evaluate_market(market_key, model_prob, odds_data):
    raw     = odds_data.get("raw", 0)
    dm_prob = odds_data.get("demargin_prob", 0)

    if raw <= 1.0 or dm_prob <= 0 or model_prob <= 0:
        return None

    edge = model_prob - dm_prob
    cat  = f"1x2_{get_odds_category(raw)}" if "1x2" in market_key else market_key
    min_edge = EDGE_MIN.get(cat, 0.05)

    if edge < min_edge:
        return None

    kf    = KELLY.get(cat, 0.20)
    stake = kelly_fraction(model_prob, raw, kf)

    return {
        "market":        market_key,
        "label":         MARKET_LABELS.get(market_key, market_key),
        "model_prob":    round(model_prob, 4),
        "demargin_prob": round(dm_prob,    4),
        "raw_odds":      raw,
        "edge":          round(edge, 4),
        "kelly_pct":     stake,
        "max_stake_pct": MAX_STAKE,
        "status":        "VALIDE",
    }


def generate_verdicts(model, odds_1x2, odds_ou25, dcs_score, moratoriums=None):
    if moratoriums is None:
        moratoriums = []

    verdicts = []

    markets_1x2 = [
        ("1x2_home", model["home"],
         odds_1x2.get("home_raw", 0), odds_1x2.get("home_prob", 0)),
        ("1x2_draw", model["draw"],
         odds_1x2.get("draw_raw", 0), odds_1x2.get("draw_prob", 0)),
        ("1x2_away", model["away"],
         odds_1x2.get("away_raw", 0), odds_1x2.get("away_prob", 0)),
    ]
    for key, model_p, raw, dm_p in markets_1x2:
        v = evaluate_market(key, model_p, {"raw": raw, "demargin_prob": dm_p})
        if v:
            verdicts.append(v)

    if "over25" not in moratoriums:
        v = evaluate_market("over25", model["over25"], {
            "raw":           odds_ou25.get("over_raw",  0),
            "demargin_prob": odds_ou25.get("over_prob", 0),
        })
        if v: verdicts.append(v)

    if "under25" not in moratoriums:
        v = evaluate_market("under25", model["under25"], {
            "raw":           odds_ou25.get("under_raw",  0),
            "demargin_prob": odds_ou25.get("under_prob", 0),
        })
        if v: verdicts.append(v)

    if dcs_score < DCS_GATE:
        for v in verdicts:
            v["status"] = "CONDITIONNEL"

    verdicts.sort(key=lambda x: -x["edge"])
    return verdicts[:4]


def format_verdict_telegram(home, away, kickoff, model, verdicts,
                             dcs_score, n_inj_home=0, n_inj_away=0,
                             odds_source="reference"):
    try:
        ko = kickoff[:16].replace("T", " ")
    except Exception:
        ko = str(kickoff)

    # Statut DCS
    dcs_flag = "OK" if dcs_score >= DCS_GATE else "CONDITIONNEL"

    lines = [
        "=" * 35,
        "  APEX-ENGINE EPL",
        "=" * 35,
        f"Match  : {home} vs {away}",
        f"Heure  : {ko} UTC",
        f"DCS    : {dcs_score:.0f}/100 [{dcs_flag}]",
        "-" * 35,
        "MODELE DIXON-COLES",
        f"xG     : {home[:12]} {model['xg_home']:.2f}",
        f"xG     : {away[:12]} {model['xg_away']:.2f}",
        f"1X2    : {model['home']:.1%} / {model['draw']:.1%} / {model['away']:.1%}",
        f"Over2.5: {model['over25']:.1%}  |  BTTS: {model['btts_yes']:.1%}",
        f"Score  : {model['modal_score'][0]}-{model['modal_score'][1]} (probable)",
        "-" * 35,
    ]

    if not verdicts:
        lines.append("DECISION : NO BET")
        lines.append("Edge insuffisant ou DCS trop bas")
    else:
        lines.append("SIGNAUX DETECTES")
        for v in verdicts:
            icon = "[OK]" if v["status"] == "VALIDE" else "[COND]"
            lines.append(
                f"{icon} {v['label']}"
            )
            lines.append(
                f"      Cote {v['raw_odds']:.2f} | "
                f"Edge +{v['edge']:.1%} | "
                f"Mise {v['max_stake_pct']:.0%} bankroll"
            )

    lines.append("-" * 35)

    # Blessés — seulement si nombre raisonnable
    inj_h_str = f"{n_inj_home}" if n_inj_home > 0 else "aucun connu"
    inj_a_str = f"{n_inj_away}" if n_inj_away > 0 else "aucun connu"
    lines.append(f"Blesses: {home[:14]} {inj_h_str}")
    lines.append(f"Blesses: {away[:14]} {inj_a_str}")
    lines.append(f"Source cotes: {odds_source}")

    if dcs_score < DCS_GATE:
        lines.append("")
        lines.append("[!] DCS bas — verifier compos H-2 avant de parier")

    lines.append("=" * 35)
    lines.append("APEX-ENGINE v1.3 | EPL 2025/26")

    return "\n".join(lines)
