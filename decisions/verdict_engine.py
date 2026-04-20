import logging

logger = logging.getLogger(__name__)

# Seuils edge minimum par marché (EPL v1.3)
EDGE_MIN = {
    "1x2_low":    0.04,   # cote 1.20-1.60
    "1x2_mid":    0.05,   # cote 1.61-2.50
    "1x2_high":   0.06,   # cote 2.51-4.00
    "1x2_xlarge": 0.09,   # cote > 4.00
    "over25":     0.05,
    "under25":    0.07,
    "btts_yes":   0.05,
    "btts_no":    0.09,
}

# Kelly fractions
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

MAX_STAKE = 0.05   # 5% bankroll max par signal
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
    """
    Évalue un marché et retourne le verdict.
    odds_data = {"raw": float, "demargin_prob": float}
    """
    raw      = odds_data.get("raw", 0)
    dm_prob  = odds_data.get("demargin_prob", 0)

    if raw <= 1.0 or dm_prob <= 0 or model_prob <= 0:
        return None

    edge = model_prob - dm_prob

    cat      = f"1x2_{get_odds_category(raw)}" if "1x2" in market_key else market_key
    min_edge = EDGE_MIN.get(cat, 0.05)

    if edge < min_edge:
        return None

    kf    = KELLY.get(cat, 0.20)
    stake = kelly_fraction(model_prob, raw, kf)

    return {
        "market":        market_key,
        "model_prob":    round(model_prob, 4),
        "demargin_prob": round(dm_prob,    4),
        "raw_odds":      raw,
        "edge":          round(edge, 4),
        "kelly_pct":     stake,
        "max_stake_pct": MAX_STAKE,
        "status":        "VALIDE",
    }


def generate_verdicts(model, odds_1x2, odds_ou25, dcs_score, moratoriums=None):
    """
    Génère tous les verdicts valides pour un match.
    model     = résultat run_simulation()
    odds_1x2  = résultat _demarginize_1x2()
    odds_ou25 = résultat _demarginize_ou()
    """
    if moratoriums is None:
        moratoriums = []

    verdicts = []

    # ── 1X2 ──────────────────────────────────────────────────────────
    markets_1x2 = [
        ("1x2_home", model["home"], odds_1x2.get("home_raw",  0),
         odds_1x2.get("home_prob", 0)),
        ("1x2_draw", model["draw"], odds_1x2.get("draw_raw",  0),
         odds_1x2.get("draw_prob", 0)),
        ("1x2_away", model["away"], odds_1x2.get("away_raw",  0),
         odds_1x2.get("away_prob", 0)),
    ]
    for key, model_p, raw, dm_p in markets_1x2:
        v = evaluate_market(key, model_p, {"raw": raw, "demargin_prob": dm_p})
        if v:
            verdicts.append(v)

    # ── Over 2.5 ─────────────────────────────────────────────────────
    if "over25" not in moratoriums:
        v = evaluate_market("over25", model["over25"], {
            "raw": odds_ou25.get("over_raw",  0),
            "demargin_prob": odds_ou25.get("over_prob", 0),
        })
        if v: verdicts.append(v)

    # ── Under 2.5 ────────────────────────────────────────────────────
    if "under25" not in moratoriums:
        v = evaluate_market("under25", model["under25"], {
            "raw": odds_ou25.get("under_raw", 0),
            "demargin_prob": odds_ou25.get("under_prob", 0),
        })
        if v: verdicts.append(v)

    # ── DCS gate ──────────────────────────────────────────────────────
    if dcs_score < DCS_GATE:
        for v in verdicts:
            v["status"] = "CONDITIONNEL"
        logger.warning(f"DCS {dcs_score} < {DCS_GATE} — signaux conditionnels")

    verdicts.sort(key=lambda x: -x["edge"])
    return verdicts[:4]


def format_verdict_telegram(home, away, kickoff, model, verdicts, dcs_score):
    """Formate le message Telegram complet."""
    ko = kickoff[:16].replace("T", " ")
    lines = [
        f"*APEX-ENGINE EPL*",
        f"*{home}* vs *{away}*",
        f"🕐 {ko} UTC | DCS: {dcs_score:.0f}/100",
        "",
        f"📊 *MODELE*",
        f"xG: {model['xg_home']:.2f} / {model['xg_away']:.2f}",
        f"1X2: {model['home']:.1%} / {model['draw']:.1%} / {model['away']:.1%}",
        f"O2.5: {model['over25']:.1%} | BTTS: {model['btts_yes']:.1%}",
        f"Score probable: {model['modal_score'][0]}-{model['modal_score'][1]}",
        "",
    ]

    if not verdicts:
        lines.append("💤 *NO BET* — Edge insuffisant")
    else:
        lines.append("🎯 *SIGNAUX*")
        emoji = {"VALIDE": "✅", "CONDITIONNEL": "⚠️"}
        for v in verdicts:
            e = emoji.get(v["status"], "⭐")
            lines.append(
                f"{e} *{v['market'].upper()}* @ {v['raw_odds']:.2f} "
                f"| Edge: +{v['edge']:.1%} "
                f"| Mise: {v['max_stake_pct']:.0%}"
            )

    if dcs_score < DCS_GATE:
        lines.append(f"\n⚠️ _DCS {dcs_score:.0f} < 70 — confirmer H-2_")

    lines.append(f"\n_APEX-ENGINE v1.3_")
    return "\n".join(lines)
