"""
APEX-ENGINE EPL v1.3 — Moteur des 11 règles contextuelles.
Chaque règle modifie les probabilités ou déclenche un moratorium.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────

EPL_DERBIES = [
    frozenset(["Manchester City",    "Manchester United"]),
    frozenset(["Arsenal",            "Tottenham"]),
    frozenset(["Liverpool",          "Everton"]),
    frozenset(["Chelsea",            "Fulham"]),
    frozenset(["Chelsea",            "Arsenal"]),
    frozenset(["Newcastle United",   "Sunderland"]),
    frozenset(["Leeds United",       "Manchester United"]),
    frozenset(["Crystal Palace",     "Brighton"]),
]

BIG_SIX = {
    "Arsenal", "Chelsea", "Liverpool",
    "Manchester City", "Manchester United", "Tottenham"
}

TIER_1 = {"Arsenal", "Manchester City", "Liverpool"}
TIER_2 = {"Chelsea", "Newcastle United", "Manchester United", "Tottenham"}
TOP_4  = TIER_1 | {"Arsenal"}  # top 4 dynamique — approximation saison

STAKE_SCORES = {
    "title":             10,
    "cl_direct":         10,
    "top4_cl":            8,
    "el_direct":          8,
    "conference":         6,
    "mid_table":          3,
    "relegation_playoff": 8,
    "relegation_direct": 10,
    "mid_secure":         2,
}


# ── Helpers ────────────────────────────────────────────────────────

def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _normalise(probs):
    total = sum(probs.values())
    if total <= 0:
        return probs
    return {k: round(v / total, 4) for k, v in probs.items()}


def _shift_toward_draw(probs, boost, from_key="home"):
    """Retire `boost` du `from_key` et l'ajoute au draw."""
    p = dict(probs)
    actual = min(boost, p.get(from_key, 0))
    p["draw"]    = _clamp(p.get("draw",    0) + actual)
    p[from_key]  = _clamp(p.get(from_key,  0) - actual)
    return _normalise(p)


def _shift_toward_away(probs, boost):
    """Retire `boost` du home et l'ajoute à l'away."""
    p = dict(probs)
    actual = min(boost, p.get("home", 0))
    p["away"] = _clamp(p.get("away", 0) + actual)
    p["home"] = _clamp(p.get("home", 0) - actual)
    return _normalise(p)


# ── Règle 1 — Moratorium Under 2.5 ────────────────────────────────

def r1_u25_moratorium(home_name, home_tier):
    """
    Tier 1 ou Tier 2 à domicile → moratorium absolu Under 2.5.
    """
    if home_tier in (1, 2):
        return {
            "blocked_market": "under25",
            "reason": f"Moratorium U2.5 — {home_name} Tier {home_tier} domicile",
        }
    return None


# ── Règle 2 — Moratorium BTTS Non ─────────────────────────────────

def r2_btts_non_moratorium(home_name, away_name, is_derby,
                            home_stake, away_stake):
    """
    Derby ou enjeu offensif élevé → moratorium BTTS Non.
    """
    if is_derby:
        return {
            "blocked_market": "btts_no",
            "reason": f"Moratorium BTTS Non — Derby EPL",
        }
    if home_stake >= 8 or away_stake >= 8:
        return {
            "blocked_market": "btts_no",
            "reason": "Moratorium BTTS Non — enjeu offensif >= 8/10",
        }
    return None


# ── Règle 3 — Derby ────────────────────────────────────────────────

def r3_derby(home_name, away_name):
    pair = frozenset([home_name, away_name])
    for derby in EPL_DERBIES:
        if pair == derby:
            return True
    return False


# ── Règle 4 — Top-of-Table Compression ─────────────────────────────

def r4_top4_compression(home_name, away_name, xg_home, xg_away):
    """
    Top 4 vs Top 4 → compression ×0.82 sur les xG.
    """
    if home_name in TOP_4 and away_name in TOP_4:
        return round(xg_home * 0.82, 3), round(xg_away * 0.82, 3), True
    return xg_home, xg_away, False


# ── Règle 5 — Big 6 Away ──────────────────────────────────────────

def r5_big6_away(away_name, away_raw_odds):
    """
    Big 6 en déplacement avec cote > 2.20 → signal value potentiel.
    """
    if away_name in BIG_SIX and away_raw_odds > 2.20:
        return {
            "flag":   "BIG6_AWAY_VALUE",
            "reason": f"{away_name} Big 6 away @ {away_raw_odds:.2f} > 2.20",
        }
    return None


# ── Règle 6 — H2H High-Score Pattern (HSP) ────────────────────────

def r6_hsp(h2h_matches, probs):
    """
    Si moy. buts 2 derniers H2H >= 3.0 → redistribution vers le draw.
    Non cumulatif avec ESY — prendre le max.
    """
    if len(h2h_matches) < 2:
        return probs, 0.0

    last2 = sorted(h2h_matches, key=lambda x: x.get("date", ""), reverse=True)[:2]
    avg_goals = sum(m.get("total_goals", 0) for m in last2) / 2

    if avg_goals >= 4.0:
        boost = 0.08
    elif avg_goals >= 3.0:
        boost = 0.05
    else:
        return probs, 0.0

    fav_key = "home" if probs["home"] >= probs["away"] else "away"
    new_probs = _shift_toward_draw(probs, boost, from_key=fav_key)
    logger.info(f"R6 HSP active: avg {avg_goals:.1f} buts H2H → +{boost:.0%} draw")
    return new_probs, boost


# ── Règle 7 — Conversion Crisis Reversal (CCR) ────────────────────

def r7_ccr(team_stats, ais_f_raw):
    """
    Ratio goals/xG < 0.70 sur 4+ matchs → plafonne AIS-F à -10%.
    Retourne l'AIS-F net plafonné et le flag.
    """
    ccr_ratio = team_stats.get("ccr_ratio", 1.0)
    matches   = team_stats.get("ccr_matches", 0)

    if matches >= 4 and ccr_ratio < 0.70:
        capped = max(ais_f_raw, -0.10)
        flag = "RÉGRESSION IMMINENTE" if matches <= 5 else "SNAP ATTENDU"
        logger.info(f"R7 CCR active: ratio {ccr_ratio:.2f} → AIS-F cappé {capped:.0%}")
        return capped, flag

    return ais_f_raw, None


# ── Règle 8 — Enjeu Symétrique (ESY) ──────────────────────────────

def r8_esy(home_stake, away_stake, probs):
    """
    Les deux enjeux >= 6/10 et non-opposés → +5% draw.
    Non cumulatif avec HSP — prendre le max.
    """
    if home_stake >= 6 and away_stake >= 6:
        # Vérifier qu'ils ne sont pas diamétralement opposés
        # (relégation vs titre = opposés → pas d'ESY)
        if not _are_opposed(home_stake, away_stake):
            boost = 0.05
            fav_key = "home" if probs["home"] >= probs["away"] else "away"
            new_probs = _shift_toward_draw(probs, boost, from_key=fav_key)
            logger.info(f"R8 ESY active: stakes {home_stake}/{away_stake} → +{boost:.0%} draw")
            return new_probs, boost

    return probs, 0.0


def _are_opposed(s1, s2):
    """Deux enjeux sont opposés si l'un gagne uniquement si l'autre perd."""
    return (s1 >= 9 and s2 <= 3) or (s2 >= 9 and s1 <= 3)


# ── Règle 9 — AIS-F Ligne Critique (ACL) ──────────────────────────

def r9_acl(lineup_data, probs, is_home_team):
    """
    >= 3 absences sur même ligne fonctionnelle → multiplicateur ACL.
    R12 (v1.4) : si ACL home Tier 1 → boost away supplémentaire.
    R13 (v1.4) : recommander DNB si ACL conditionnel.
    """
    if not lineup_data:
        return probs, 1.0, None, False

    acl_info = lineup_data.get("acl_check", {})
    if not acl_info.get("active"):
        return probs, 1.0, None, False

    factor  = acl_info.get("factor", 1.0)
    flag    = acl_info.get("flag", "LIGNE_CRITIQUE")
    prefer_dnb = factor < 2.0  # R13

    if is_home_team:
        # ACL équipe domicile → avantage away
        if factor >= 2.0:
            shift = 0.12
        else:
            shift = 0.07

        frac_away = 0.60
        frac_draw = 0.40
        p = dict(probs)
        available = min(shift, p.get("home", 0))
        p["away"] = _clamp(p.get("away", 0) + available * frac_away)
        p["draw"] = _clamp(p.get("draw", 0) + available * frac_draw)
        p["home"] = _clamp(p.get("home", 0) - available)
        new_probs = _normalise(p)

    else:
        # ACL équipe away → avantage home
        if factor >= 2.0:
            shift = 0.10
        else:
            shift = 0.06
        new_probs = _shift_toward_away(
            {**probs, "home": probs["away"], "away": probs["home"]},
            shift
        )
        # Remettre dans le bon sens
        new_probs = {"home": new_probs["away"],
                     "draw": new_probs["draw"],
                     "away": new_probs["home"]}
        new_probs = _normalise(new_probs)

    logger.info(f"R9 ACL active: factor {factor} flag={flag}")
    return new_probs, factor, flag, prefer_dnb


# ── Règle 10 — Trauma Européen (TE) ────────────────────────────────

def r10_te(te_goals_conceded, te_hours_before, probs, is_home_team):
    """
    >= 5 buts concédés en euro dans les 96h → coefficient TE.
    """
    if te_goals_conceded < 5 or te_hours_before > 96:
        return probs, False

    if te_goals_conceded >= 7:
        away_boost = 0.10
    else:
        away_boost = 0.06

    # TE réduit si > 72h ou si derby
    if te_hours_before > 72:
        away_boost = 0.04

    if is_home_team:
        # L'équipe traumatisée est à domicile → boost away
        p = dict(probs)
        actual = min(away_boost, p.get("home", 0))
        p["away"] = _clamp(p.get("away", 0) + actual)
        p["home"] = _clamp(p.get("home", 0) - actual)
        new_probs = _normalise(p)
    else:
        # L'équipe traumatisée est à l'extérieur → boost home
        p = dict(probs)
        actual = min(away_boost, p.get("away", 0))
        p["home"] = _clamp(p.get("home", 0) + actual)
        p["away"] = _clamp(p.get("away", 0) - actual)
        new_probs = _normalise(p)

    logger.info(f"R10 TE active: {te_goals_conceded} buts concédés → boost {away_boost:.0%}")
    return new_probs, True


# ── Règle 11 — Europa Bounce Away (EBA) ────────────────────────────

def r11_eba(eba_active, eba_victory, rotations, xg_away):
    """
    Victoire coupe + rotation >= 4 titulaires <= 72h → bonus xG away.
    """
    if not eba_active or not eba_victory:
        return xg_away, False

    if rotations >= 4:
        bonus = 1.10
        logger.info(f"R11 EBA active: victoire coupe + {rotations} rotations → xG away ×{bonus}")
        return round(xg_away * bonus, 3), True

    return xg_away, False


# ── Orchestrateur principal ─────────────────────────────────────────

def apply_all_rules(match_ctx, probs, xg_home, xg_away):
    """
    Applique les 11 règles dans l'ordre.

    match_ctx = {
        "home_name", "away_name",
        "home_tier", "away_tier",
        "home_stake", "away_stake",
        "home_lineup", "away_lineup",
        "h2h_matches",
        "home_te_goals", "home_te_hours",
        "away_te_goals", "away_te_hours",
        "away_eba_active", "away_eba_victory", "away_eba_rotations",
        "home_raw_odds", "away_raw_odds",
        "home_stats", "away_stats",
        "home_ais_f_raw", "away_ais_f_raw",
    }

    Retourne (probs_ajustées, moratoriums, rules_active, xg_home, xg_away)
    """
    p          = dict(probs)
    moratoriums = []
    rules_active = []

    home = match_ctx.get("home_name", "")
    away = match_ctx.get("away_name", "")

    # ── R3 : Derby ────────────────────────────────────────────────
    is_derby = r3_derby(home, away)
    if is_derby:
        rules_active.append("R3_DERBY")

    # ── R4 : Top-4 compression ────────────────────────────────────
    xg_home, xg_away, is_top4 = r4_top4_compression(home, away, xg_home, xg_away)
    if is_top4:
        rules_active.append("R4_TOP4_COMPRESSION")

    # ── R11 : EBA ─────────────────────────────────────────────────
    xg_away, eba_ok = r11_eba(
        match_ctx.get("away_eba_active",    False),
        match_ctx.get("away_eba_victory",   False),
        match_ctx.get("away_eba_rotations", 0),
        xg_away,
    )
    if eba_ok:
        rules_active.append("R11_EBA_BOUNCE")

    # ── R10 : TE home ─────────────────────────────────────────────
    p, te_home = r10_te(
        match_ctx.get("home_te_goals", 0),
        match_ctx.get("home_te_hours", 999),
        p, is_home_team=True,
    )
    if te_home:
        rules_active.append(f"R10_TE_HOME ({match_ctx.get('home_te_goals')} buts)")

    # ── R10 : TE away ─────────────────────────────────────────────
    p, te_away = r10_te(
        match_ctx.get("away_te_goals", 0),
        match_ctx.get("away_te_hours", 999),
        p, is_home_team=False,
    )
    if te_away:
        rules_active.append(f"R10_TE_AWAY ({match_ctx.get('away_te_goals')} buts)")

    # ── R9 : ACL home ─────────────────────────────────────────────
    p, acl_factor_h, acl_flag_h, prefer_dnb_h = r9_acl(
        match_ctx.get("home_lineup"), p, is_home_team=True
    )
    if acl_flag_h:
        rules_active.append(f"R9_ACL_HOME [{acl_flag_h}]")

    # ── R9 : ACL away ─────────────────────────────────────────────
    p, acl_factor_a, acl_flag_a, prefer_dnb_a = r9_acl(
        match_ctx.get("away_lineup"), p, is_home_team=False
    )
    if acl_flag_a:
        rules_active.append(f"R9_ACL_AWAY [{acl_flag_a}]")

    # ── R6 : HSP ──────────────────────────────────────────────────
    p, hsp_boost = r6_hsp(match_ctx.get("h2h_matches", []), p)
    if hsp_boost > 0:
        rules_active.append(f"R6_HSP (+{hsp_boost:.0%} draw)")

    # ── R8 : ESY ──────────────────────────────────────────────────
    p, esy_boost = r8_esy(
        match_ctx.get("home_stake", 3),
        match_ctx.get("away_stake", 3),
        p,
    )
    if esy_boost > 0:
        rules_active.append(f"R8_ESY (+{esy_boost:.0%} draw)")

    # ── HSP + ESY : non-cumulatif → reprendre le max ──────────────
    if hsp_boost > 0 and esy_boost > 0:
        max_boost = max(hsp_boost, esy_boost)
        fav_key   = "home" if probs["home"] >= probs["away"] else "away"
        p_reset   = dict(probs)
        p_reset["draw"]   = _clamp(p_reset.get("draw", 0)   + max_boost)
        p_reset[fav_key]  = _clamp(p_reset.get(fav_key, 0)  - max_boost)
        p = _normalise(p_reset)
        rules_active = [r for r in rules_active
                        if "R6_HSP" not in r and "R8_ESY" not in r]
        rules_active.append(f"R6+R8_MAX_BOOST (+{max_boost:.0%} draw)")

    # ── R1 : Moratorium U2.5 ──────────────────────────────────────
    m1 = r1_u25_moratorium(home, match_ctx.get("home_tier", 4))
    if m1:
        moratoriums.append(m1)

    # ── R2 : Moratorium BTTS Non ──────────────────────────────────
    m2 = r2_btts_non_moratorium(
        home, away, is_derby,
        match_ctx.get("home_stake", 3),
        match_ctx.get("away_stake", 3),
    )
    if m2:
        moratoriums.append(m2)

    # ── R3 : Derby → moratorium BTTS Non + U2.5 ───────────────────
    if is_derby:
        moratoriums.append({
            "blocked_market": "under25",
            "reason": "Derby EPL — pas de marché défensif",
        })

    # ── R5 : Big 6 Away ───────────────────────────────────────────
    r5 = r5_big6_away(away, match_ctx.get("away_raw_odds", 1.5))
    if r5:
        rules_active.append(f"R5_{r5['flag']}")

    # ── R14 (v1.4) : MSRD ─────────────────────────────────────────
    # Survie relégation domicile réduit impact ACL de 25%
    home_stake = match_ctx.get("home_stake", 3)
    if acl_flag_h and home_stake >= 9:
        # Atténuer l'effet ACL de 25% en rapprochant des probs originales
        for k in p:
            p[k] = round(probs[k] + (p[k] - probs[k]) * 0.75, 4)
        p = _normalise(p)
        rules_active.append("R14_MSRD (ACL réduit 25% — survie domicile)")

    # ── R15 (v1.4) : seuil edge réduit si ≥2 règles v1.3 ─────────
    v13_rules = [r for r in rules_active
                 if any(tag in r for tag in ["R6", "R8", "R9", "R10", "R11"])]
    multi_rule_active = len(v13_rules) >= 2

    p = _normalise(p)

    return p, moratoriums, rules_active, xg_home, xg_away, multi_rule_active
