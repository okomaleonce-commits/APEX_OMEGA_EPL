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

MARKET_LABELS = {
    "1x2_home": "Victoire Domicile",
    "1x2_draw": "Match Nul",
    "1x2_away": "Victoire Exterieur",
    "over25":   "Over 2.5 buts",
    "under25":  "Under 2.5 buts",
    "btts_yes": "BTTS Oui",
    "btts_no":  "BTTS Non",
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


def evaluate_market(market_key, model_prob, odds_data,
                    moratoriums=None, r15_active=False):
    if moratoriums is None:
        moratoriums = []

    # Verifier moratorium
    if market_key in moratoriums:
        return None

    raw     = odds_data.get("raw", 0)
    dm_prob = odds_data.get("demargin_prob", 0)

    if raw <= 1.0 or dm_prob <= 0 or model_prob <= 0:
        return None

    edge = model_prob - dm_prob
    cat  = f"1x2_{get_odds_category(raw)}" if "1x2" in market_key else market_key
    min_edge = EDGE_MIN.get(cat, 0.05)

    # R15 : reduire seuil si >= 2 regles v1.3 actives
    if r15_active and cat == "1x2_high":
        min_edge = 0.05

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


def generate_verdicts(model, odds_1x2, odds_ou25, dcs_score,
                      moratoriums=None, r15_active=False):
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
        v = evaluate_market(
            key, model_p,
            {"raw": raw, "demargin_prob": dm_p},
            moratoriums=moratoriums,
            r15_active=r15_active,
        )
        if v: verdicts.append(v)

    v = evaluate_market(
        "over25", model["over25"],
        {"raw": odds_ou25.get("over_raw", 0),
         "demargin_prob": odds_ou25.get("over_prob", 0)},
        moratoriums=moratoriums,
    )
    if v: verdicts.append(v)

    v = evaluate_market(
        "under25", model["under25"],
        {"raw": odds_ou25.get("under_raw", 0),
         "demargin_prob": odds_ou25.get("under_prob", 0)},
        moratoriums=moratoriums,
    )
    if v: verdicts.append(v)

    if dcs_score < DCS_GATE:
        for v in verdicts:
            v["status"] = "CONDITIONNEL"

    verdicts.sort(key=lambda x: -x["edge"])
    return verdicts[:4]


def format_verdict_telegram(home, away, kickoff, model, verdicts,
                             dcs_score, n_inj_home=0, n_inj_away=0,
                             odds_source="reference",
                             rules_active=None, moratoriums=None):
    if rules_active is None:
        rules_active = []
    if moratoriums is None:
        moratoriums = []

    try:
        ko = kickoff[:16].replace("T", " ")
    except Exception:
        ko = str(kickoff)

    dcs_flag = "OK" if dcs_score >= DCS_GATE else "COND"

    lines = [
        "=" * 38,
        "   APEX-ENGINE EPL v1.3",
        "=" * 38,
        f"Match  : {home}",
        f"         vs {away}",
        f"Heure  : {ko} UTC",
        f"DCS    : {dcs_score:.0f}/100 [{dcs_flag}]",
        "-" * 38,
        "MODELE DIXON-COLES + Monte Carlo 50k",
        f"xG Home: {model['xg_home']:.2f}  |  xG Away: {model['xg_away']:.2f}",
        f"1X2    : {model['home']:.1%} / {model['draw']:.1%} / {model['away']:.1%}",
        f"Over2.5: {model['over25']:.1%}  |  BTTS Oui: {model['btts_yes']:.1%}",
        f"Score  : {model['modal_score'][0]}-{model['modal_score'][1]} (probable)",
        "-" * 38,
    ]

    # Regles actives
    if rules_active:
        lines.append("REGLES ACTIVES v1.3")
        for r in rules_active[:6]:
            lines.append(f"  [{r[:40]}]")
        lines.append("")

    # Moratoriums
    if moratoriums:
        lines.append("MORATORIUMS")
        for m in moratoriums[:3]:
            lines.append(f"  [BLOQUE] {m['market']} — {m['reason'][:35]}")
        lines.append("")

    lines.append("-" * 38)

    # Verdicts
    if not verdicts:
        lines.append("DECISION : NO BET")
        lines.append("Edge insuffisant ou marche bloque")
    else:
        lines.append("SIGNAUX DETECTES")
        for v in verdicts:
            icon = "[OK]" if v["status"] == "VALIDE" else "[COND]"
            lines.append(f"")
            lines.append(f"{icon} {v['label']}")
            lines.append(
                f"  Cote {v['raw_odds']:.2f} | "
                f"Edge +{v['edge']:.1%} | "
                f"Mise {v['max_stake_pct']:.0%}"
            )

    lines.append("-" * 38)

    inj_h = str(n_inj_home) if n_inj_home > 0 else "aucun connu"
    inj_a = str(n_inj_away) if n_inj_away > 0 else "aucun connu"
    lines.append(f"Blesses: {home[:15]:15} {inj_h}")
    lines.append(f"Blesses: {away[:15]:15} {inj_a}")
    lines.append(f"Source cotes: {odds_source}")

    if dcs_score < DCS_GATE:
        lines.append("")
        lines.append("[!] DCS bas — verifier compos H-2 avant de parier")

    lines.append("=" * 38)
    lines.append("APEX-ENGINE v1.3 | EPL 2025/26")

    return "\n".join(lines)
