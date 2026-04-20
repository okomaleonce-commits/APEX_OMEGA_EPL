"""
APEX-ENGINE EPL v1.3 — Moteur des 11 regles contextuelles
Applique les regles dans l'ordre et retourne :
- probs ajustees {home, draw, away}
- liste des regles actives
- liste des moratoriums bloques
"""
import logging

logger = logging.getLogger(__name__)

# ── Parametres des regles ─────────────────────────────────────────

# Tiers EPL
TIER_MAP = {
    "Arsenal": 1, "Manchester City": 1, "Liverpool": 1,
    "Chelsea": 2, "Newcastle United": 2, "Manchester United": 2,
    "Tottenham": 2, "Tottenham Hotspur": 2,
    "Aston Villa": 3, "Brighton": 3, "Brighton & Hove Albion": 3,
    "Fulham": 3, "West Ham": 3, "West Ham United": 3,
    "Bournemouth": 3, "AFC Bournemouth": 3,
    "Brentford": 4, "Crystal Palace": 4,
    "Everton": 4, "Wolverhampton": 4, "Wolves": 4,
    "Burnley": 5, "Sunderland": 5, "Leeds": 5, "Leeds United": 5,
    "Nottingham Forest": 5, "Southampton": 5,
}

BIG_SIX = {
    "Arsenal", "Chelsea", "Liverpool",
    "Manchester City", "Manchester United",
    "Tottenham", "Tottenham Hotspur"
}

DERBIES = [
    {"Arsenal", "Tottenham"}, {"Arsenal", "Tottenham Hotspur"},
    {"Liverpool", "Everton"},
    {"Manchester United", "Manchester City"},
    {"Chelsea", "Fulham"},
    {"Crystal Palace", "Brighton"}, {"Crystal Palace", "Brighton & Hove Albion"},
]

# Scores d'enjeu /10
STAKE_SCORES = {
    "title":              10,
    "cl_direct":          10,
    "top4_cl":             8,
    "el_direct":           8,
    "conference":          6,
    "mid_table":           3,
    "relegation_playoff":  8,
    "relegation_direct":  10,
    "secure":              2,
}


def get_tier(team_name):
    for k, v in TIER_MAP.items():
        if k.lower() in team_name.lower() or team_name.lower() in k.lower():
            return v
    return 4  # default mid-table


def is_derby(home, away):
    pair = {home, away}
    for d in DERBIES:
        if pair == d or (home in d and away in d):
            return True
    return False


def is_big6(team):
    return any(b.lower() in team.lower() for b in BIG_SIX)


def apply_all_rules(home_name, away_name, probs, context):
    """
    Applique les 11 regles v1.3.

    context = {
        # Donnees de base
        "home_tier": int,
        "away_tier": int,
        "venue_capacity": int,

        # Fatigue / calendrier
        "home_fatigue_coeff": float,  # deja applique sur xG
        "away_fatigue_coeff": float,

        # Blessures
        "home_inj_count": int,
        "away_inj_count": int,
        "home_acl_active": bool,   # >= 3 joueurs meme ligne
        "away_acl_active": bool,
        "home_acl_factor": float,  # 1.5 ou 2.0
        "away_acl_factor": float,

        # Regles UCL/UEL
        "te_home": bool,           # Trauma Europeen home
        "te_away": bool,
        "te_goals": int,           # buts encaisses en euro
        "eba_away": bool,          # Europa Bounce Away
        "eba_home": bool,

        # H2H
        "h2h_avg_goals": float,

        # CCR
        "home_ccr_ratio": float,   # goals/xG
        "away_ccr_ratio": float,

        # Enjeux
        "home_stake_score": int,
        "away_stake_score": int,
    }

    Retourne: (probs_ajustees, moratoriums, rules_active)
    """
    adj        = probs.copy()
    moratoriums = []
    rules_active = []

    home_tier = context.get("home_tier", get_tier(home_name))
    away_tier = context.get("away_tier", get_tier(away_name))
    derby     = is_derby(home_name, away_name)

    # ── R1 : Moratorium Under 2.5 ────────────────────────────────
    if home_tier in (1, 2):
        moratoriums.append({
            "rule":   "R1_UNDER25",
            "market": "under25",
            "reason": f"Tier {home_tier} a domicile — moratorium absolu",
        })

    # ── R2 : Moratorium BTTS Non ─────────────────────────────────
    if derby or home_tier == 1:
        moratoriums.append({
            "rule":   "R2_BTTS_NON",
            "market": "btts_no",
            "reason": "Derby ou Tier 1 — BTTS Non trop risque",
        })

    # ── R3 : Derby ───────────────────────────────────────────────
    if derby:
        moratoriums.append({
            "rule":   "R3_DERBY",
            "market": "under25",
            "reason": "Derby EPL — variance emotionnelle maximale",
        })
        moratoriums.append({
            "rule":   "R3_DERBY",
            "market": "btts_no",
            "reason": "Derby EPL — pas de marche defensif",
        })
        rules_active.append("R3_DERBY")
        logger.info(f"R3 Derby detecte: {home_name} vs {away_name}")

    # ── R4 : Top-4 Compression ───────────────────────────────────
    if home_tier <= 2 and away_tier <= 2:
        rules_active.append("R4_TOP4_COMPRESSION")
        # Deja applique via xG compression dans compute_xg
        logger.info("R4 Top4 compression active")

    # ── R5 : Big 6 Away ──────────────────────────────────────────
    if is_big6(away_name) and away_tier >= 3:
        rules_active.append("R5_BIG6_AWAY")
        logger.info(f"R5 Big6 Away: {away_name} en deplacement")

    # ── R6 : H2H High-Score Pattern (HSP) ────────────────────────
    h2h_avg = context.get("h2h_avg_goals", 0)
    hsp_boost = 0
    if h2h_avg >= 4.0:
        hsp_boost = 0.08
    elif h2h_avg >= 3.0:
        hsp_boost = 0.05

    if hsp_boost > 0:
        fav = max(adj, key=adj.get)
        adj["draw"] = adj.get("draw", 0) + hsp_boost
        adj[fav]    = max(0, adj.get(fav, 0) - hsp_boost)
        rules_active.append(f"R6_HSP +{hsp_boost:.0%} draw (H2H moy {h2h_avg:.1f} buts)")
        logger.info(f"R6 HSP: +{hsp_boost:.0%} draw")

    # ── R7 : CCR (Conversion Crisis Reversal) ────────────────────
    home_ccr = context.get("home_ccr_ratio", 1.0)
    away_ccr = context.get("away_ccr_ratio", 1.0)
    if home_ccr < 0.70:
        rules_active.append(f"R7_CCR_HOME ratio={home_ccr:.2f} — REGRESSION IMMINENTE")
    if away_ccr < 0.70:
        rules_active.append(f"R7_CCR_AWAY ratio={away_ccr:.2f} — SNAP ATTENDU")

    # ── R8 : Enjeu Symetrique (ESY) ──────────────────────────────
    hs = context.get("home_stake_score", 3)
    as_ = context.get("away_stake_score", 3)
    esy_boost = 0
    if hs >= 6 and as_ >= 6:
        esy_boost = 0.05
        fav = max(adj, key=adj.get)
        adj["draw"] = adj.get("draw", 0) + esy_boost
        adj[fav]    = max(0, adj.get(fav, 0) - esy_boost)
        rules_active.append(f"R8_ESY +5% draw (enjeux {hs}/10 vs {as_}/10)")
        logger.info(f"R8 ESY: +5% draw")

    # R6 + R8 non cumulatifs — prendre le max
    if hsp_boost > 0 and esy_boost > 0:
        # Annuler le plus petit et ne garder que le max
        min_boost = min(hsp_boost, esy_boost)
        fav = max(adj, key=adj.get)
        adj["draw"] = adj.get("draw", 0) - min_boost
        adj[fav]    = adj.get(fav, 0) + min_boost
        rules_active.append(f"R6+R8 non-cumulatif — max garde")

    # ── R9 : ACL (AIS-F Ligne Critique) ──────────────────────────
    if context.get("home_acl_active"):
        factor = context.get("home_acl_factor", 1.5)
        if factor >= 2.0:
            # Effondrement ligne : fort boost away
            adj["away"] = adj.get("away", 0) + 0.12
            adj["home"] = max(0, adj.get("home", 0) - 0.08)
            adj["draw"] = max(0, adj.get("draw", 0) - 0.04)
            rules_active.append("R9_ACL_HOME x2.0 EFFONDREMENT LIGNE")
        else:
            adj["away"] = adj.get("away", 0) + 0.07
            adj["home"] = max(0, adj.get("home", 0) - 0.05)
            adj["draw"] = max(0, adj.get("draw", 0) - 0.02)
            # R12 v1.4 : si Tier 1 home + ACL -> extra boost away
            if home_tier == 1:
                adj["away"] = adj.get("away", 0) + 0.08
                adj["home"] = max(0, adj.get("home", 0) - 0.08)
                rules_active.append("R12_ACL_TIER1_HOME +8% away (v1.4)")
            rules_active.append("R9_ACL_HOME x1.5 LIGNE CRITIQUE")

    if context.get("away_acl_active"):
        factor = context.get("away_acl_factor", 1.5)
        if factor >= 2.0:
            adj["home"] = adj.get("home", 0) + 0.10
            adj["away"] = max(0, adj.get("away", 0) - 0.07)
            adj["draw"] = max(0, adj.get("draw", 0) - 0.03)
            rules_active.append("R9_ACL_AWAY x2.0 EFFONDREMENT LIGNE")
        else:
            adj["home"] = adj.get("home", 0) + 0.06
            adj["away"] = max(0, adj.get("away", 0) - 0.04)
            adj["draw"] = max(0, adj.get("draw", 0) - 0.02)
            # R14 v1.4 : si home en zone relégation + ACL away -> reduire impact
            if hs >= 9:
                reduction = 0.25
                for k in adj:
                    adj[k] = probs[k] + (adj[k] - probs[k]) * (1 - reduction)
                rules_active.append("R14_MSRD relégation domicile reduit ACL 25%")
            rules_active.append("R9_ACL_AWAY x1.5 LIGNE CRITIQUE")

    # ── R10 : Trauma Europeen (TE) ────────────────────────────────
    if context.get("te_home"):
        te_goals = context.get("te_goals", 5)
        if te_goals >= 7:
            adj["away"] = adj.get("away", 0) + 0.10
            adj["home"] = max(0, adj.get("home", 0) - 0.10)
            rules_active.append(f"R10_TE_HOME {te_goals} buts encaisses — TRAUMA x0.75")
        else:
            adj["away"] = adj.get("away", 0) + 0.06
            adj["home"] = max(0, adj.get("home", 0) - 0.06)
            rules_active.append(f"R10_TE_HOME {te_goals} buts encaisses — x0.85")

    if context.get("te_away"):
        te_goals = context.get("te_goals", 5)
        if te_goals >= 7:
            adj["home"] = adj.get("home", 0) + 0.10
            adj["away"] = max(0, adj.get("away", 0) - 0.10)
            rules_active.append(f"R10_TE_AWAY {te_goals} buts encaisses — TRAUMA x0.75")
        else:
            adj["home"] = adj.get("home", 0) + 0.06
            adj["away"] = max(0, adj.get("away", 0) - 0.06)
            rules_active.append(f"R10_TE_AWAY {te_goals} buts encaisses — x0.85")

    # ── R11 : Europa Bounce Away (EBA) ───────────────────────────
    if context.get("eba_away"):
        adj["away"] = adj.get("away", 0) + 0.05
        adj["home"] = max(0, adj.get("home", 0) - 0.05)
        rules_active.append("R11_EBA_AWAY victoire coupe + rotation — +5% away")

    if context.get("eba_home"):
        adj["home"] = adj.get("home", 0) + 0.03
        adj["away"] = max(0, adj.get("away", 0) - 0.03)
        rules_active.append("R11_EBA_HOME victoire coupe + rotation — +3% home")

    # ── R15 v1.4 : Seuil edge reduit si >= 2 regles v1.3 actives ─
    rules_v13 = [r for r in rules_active
                 if any(tag in r for tag in
                        ["R6_", "R7_", "R8_", "R9_", "R10_", "R11_"])]
    if len(rules_v13) >= 2:
        rules_active.append("R15_MULTI_RULE seuil edge 6%->5% (>= 2 regles v1.3)")

    # ── Normaliser les probs ──────────────────────────────────────
    total = sum(adj.values())
    if total > 0:
        adj = {k: round(v / total, 4) for k, v in adj.items()}

    return adj, moratoriums, rules_active
