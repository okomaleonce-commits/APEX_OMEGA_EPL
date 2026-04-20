import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Paramètres EPL calibrés v1.3
AVG_GOALS   = 2.89
AVG_TEAM    = 1.445
HOME_ADV    = 1.08
RHO         = 0.08
MONTE_CARLO = 50_000

RNG = np.random.default_rng(seed=42)


def tau(h, a, mu, la, rho):
    if h == 0 and a == 0:
        return max(0, 1 - mu * la * rho)
    elif h == 1 and a == 0:
        return 1 + la * rho
    elif h == 0 and a == 1:
        return 1 + mu * rho
    elif h == 1 and a == 1:
        return max(0, 1 - rho)
    return 1.0


def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def compute_xg(home_stats, away_stats, home_capacity=0,
               fatigue_home=1.0, fatigue_away=1.0,
               ais_f_home=0.0, ais_f_away=0.0):
    """
    Calcule les xG finaux home et away.
    Applique : attack/defense rates, HOME_ADV, fatigue, AIS-F.
    """
    avg = AVG_TEAM

    gf_home = home_stats.get("goals_scored_avg",   avg)
    ga_home = home_stats.get("goals_conceded_avg",  avg)
    gf_away = away_stats.get("goals_scored_avg",   avg)
    ga_away = away_stats.get("goals_conceded_avg",  avg)

    attack_home  = gf_home / avg
    defense_away = ga_away / avg
    attack_away  = gf_away / avg
    defense_home = ga_home / avg

    # Grand stade → HOME_ADV = 1.10
    home_adv = 1.10 if home_capacity >= 50_000 else HOME_ADV

    xg_home = attack_home * defense_away * avg * home_adv
    xg_away = attack_away * defense_home * avg

    # Fatigue (Situation A/B)
    xg_home *= fatigue_home
    xg_away *= fatigue_away

    # AIS-F (pénalité absences, négatif)
    xg_home = xg_home * (1 + ais_f_home)
    xg_away = xg_away * (1 + ais_f_away)

    # Guards
    xg_home = max(round(xg_home, 3), 0.20)
    xg_away = max(round(xg_away, 3), 0.10)

    return xg_home, xg_away


def run_simulation(xg_home, xg_away, n=MONTE_CARLO):
    """
    Monte-Carlo Dixon-Coles 50k itérations.
    Retourne probabilités 1X2, Over/Under 2.5, BTTS.
    """
    home_goals = RNG.poisson(xg_home, n)
    away_goals = RNG.poisson(xg_away, n)

    # Poids Dixon-Coles
    weights = np.ones(n)
    for i in range(n):
        h, a = int(home_goals[i]), int(away_goals[i])
        t = tau(h, a, xg_home, xg_away, RHO)
        weights[i] = max(t, 0)

    total_w = weights.sum()
    if total_w == 0:
        total_w = 1

    home_win = weights[(home_goals > away_goals)].sum() / total_w
    draw     = weights[(home_goals == away_goals)].sum() / total_w
    away_win = weights[(home_goals < away_goals)].sum() / total_w
    over25   = weights[(home_goals + away_goals) > 2].sum() / total_w
    btts_yes = weights[(home_goals > 0) & (away_goals > 0)].sum() / total_w

    # Score modal
    counts = {}
    for i in range(n):
        s = (int(home_goals[i]), int(away_goals[i]))
        counts[s] = counts.get(s, 0) + weights[i]

    top5 = sorted(counts.items(), key=lambda x: -x[1])[:5]
    modal = top5[0][0] if top5 else (1, 1)

    return {
        "xg_home": xg_home,
        "xg_away": xg_away,
        "home":    round(float(home_win), 4),
        "draw":    round(float(draw),     4),
        "away":    round(float(away_win), 4),
        "over25":  round(float(over25),   4),
        "under25": round(1 - float(over25), 4),
        "btts_yes": round(float(btts_yes), 4),
        "btts_no":  round(1 - float(btts_yes), 4),
        "modal_score": modal,
        "top5_scores": [(str(s), round(float(p/total_w), 4)) for s, p in top5],
    }
