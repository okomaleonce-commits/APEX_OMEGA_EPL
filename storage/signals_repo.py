import os
import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("RENDER_DISK_PATH", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "apex_epl.db"


def _sanitize(value, max_len: int = 500) -> str:
    """
    Sanitise une valeur avant insertion en DB.
    Retire les caractères de contrôle et tronque si nécessaire.
    """
    if value is None:
        return ""
    s = str(value)
    # Retirer caractères de contrôle (sauf tab/newline)
    s = re.sub(r"[--]", "", s)
    return s[:max_len]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id    INTEGER,
                kickoff_utc   TEXT,
                home_team     TEXT,
                away_team     TEXT,
                market        TEXT,
                outcome       TEXT,
                model_prob    REAL,
                demargin_prob REAL,
                raw_odds      REAL,
                edge          REAL,
                max_stake_pct REAL,
                status        TEXT,
                dcs_score     REAL,
                xg_home       REAL,
                xg_away       REAL,
                odds_source   TEXT,
                created_at    TEXT,
                resolved      INTEGER DEFAULT 0,
                result        TEXT,
                pnl_pct       REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_outcomes (
                fixture_id  INTEGER PRIMARY KEY,
                home_team   TEXT,
                away_team   TEXT,
                kickoff_utc TEXT,
                home_goals  INTEGER,
                away_goals  INTEGER,
                over25      INTEGER,
                btts        INTEGER,
                fetched_at  TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at     TEXT,
                fixtures_n INTEGER,
                signals_n  INTEGER,
                status     TEXT
            )
        """)
        conn.commit()
    logger.info(f"DB initialisee: {DB_PATH}")


def save_signal(data):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("""
            INSERT INTO signals (
                fixture_id, kickoff_utc, home_team, away_team,
                market, outcome, model_prob, demargin_prob,
                raw_odds, edge, max_stake_pct, status,
                dcs_score, xg_home, xg_away, odds_source, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("fixture_id", 0),
            data.get("kickoff_utc", ""),
            data.get("home_team", ""),
            data.get("away_team", ""),
            data.get("market", ""),
            data.get("outcome", ""),
            data.get("model_prob", 0),
            data.get("demargin_prob", 0),
            data.get("raw_odds", 0),
            data.get("edge", 0),
            data.get("max_stake_pct", 0),
            data.get("status", ""),
            data.get("dcs_score", 0),
            data.get("xg_home", 0),
            data.get("xg_away", 0),
            data.get("odds_source", ""),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        return cursor.lastrowid


def save_no_bet(fixture_id, home, away, kickoff, dcs, xg_home, xg_away):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO signals (
                fixture_id, kickoff_utc, home_team, away_team,
                market, status, dcs_score, xg_home, xg_away, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            fixture_id, kickoff, home, away,
            "NO_BET", "NO_BET", dcs, xg_home, xg_away,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()


def resolve_signal(signal_id, result, pnl):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE signals
            SET resolved = 1, result = ?, pnl_pct = ?
            WHERE id = ?
        """, (result, pnl, signal_id))
        conn.commit()


def save_outcome(fixture_id, home, away, kickoff, home_goals, away_goals):
    over25 = 1 if (home_goals + away_goals) > 2 else 0
    btts   = 1 if (home_goals > 0 and away_goals > 0) else 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO match_outcomes
            (fixture_id, home_team, away_team, kickoff_utc,
             home_goals, away_goals, over25, btts, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            fixture_id, home, away, kickoff,
            home_goals, away_goals, over25, btts,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()


def get_unresolved_signals():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM signals
            WHERE resolved = 0
            AND status IN ('VALIDE', 'CONDITIONNEL')
            AND kickoff_utc < datetime('now', '-2 hours')
        """).fetchall()
        return [dict(r) for r in rows]


def log_pipeline_run(fixtures_n, signals_n, status="OK"):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO pipeline_runs (run_at, fixtures_n, signals_n, status)
            VALUES (?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            fixtures_n, signals_n, status
        ))
        conn.commit()


def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total    = conn.execute("SELECT COUNT(*) as n FROM signals WHERE status != 'NO_BET'").fetchone()["n"]
        resolved = conn.execute("SELECT COUNT(*) as n FROM signals WHERE resolved = 1").fetchone()["n"]
        wins     = conn.execute("SELECT COUNT(*) as n FROM signals WHERE result = 'WIN'").fetchone()["n"]
        losses   = conn.execute("SELECT COUNT(*) as n FROM signals WHERE result = 'LOSS'").fetchone()["n"]
        pnl_row  = conn.execute("SELECT SUM(pnl_pct) as s FROM signals WHERE resolved = 1").fetchone()
        pnl      = round(pnl_row["s"] or 0, 4)

        return {
            "total_signals":    total,
            "resolved":         resolved,
            "wins":             wins,
            "losses":           losses,
            "pnl_pct":          pnl,
            "win_rate":         round(wins / resolved, 3) if resolved > 0 else 0,
        }
