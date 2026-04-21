import os
import logging
import sqlite3
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def handle_start(bot, chat_id):
    msg = (
        "APEX-ENGINE EPL v1.3\n"
        "======================\n"
        "Bot d'analyse EPL actif.\n\n"
        "Commandes :\n"
        "/status  — Etat du bot\n"
        "/analyse — Lancer une analyse\n"
        "/bilan   — P&L performance\n"
        "/api     — Etat des APIs\n"
        "/help    — Aide"
    )
    await bot.send_message(chat_id=chat_id, text=msg)


async def handle_status(bot, chat_id):
    from storage.signals_repo import get_stats, DB_PATH
    import os
    stats   = get_stats()
    db_size = DB_PATH.stat().st_size / 1024 if DB_PATH.exists() else 0
    now     = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
    msg = (
        "=== STATUS APEX-ENGINE ===\n"
        f"Date     : {now} UTC\n"
        f"DB       : {db_size:.1f} KB\n"
        f"Signaux  : {stats['total_signals']}\n"
        f"Resolus  : {stats['resolved']}\n"
        f"Victoires: {stats['wins']}\n"
        f"Defaites : {stats['losses']}\n"
        f"Win rate : {stats['win_rate']:.1%}\n"
        f"P&L net  : {stats['pnl_pct']:+.1%}\n"
        "========================="
    )
    await bot.send_message(chat_id=chat_id, text=msg)


async def handle_analyse(bot, chat_id, run_pipeline_fn):
    await bot.send_message(
        chat_id=chat_id,
        text="Analyse en cours... resultats dans quelques secondes."
    )
    try:
        await run_pipeline_fn()
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"Erreur: {e}")


async def handle_bilan(bot, chat_id):
    from storage.signals_repo import get_stats, DB_PATH
    import os
    stats   = get_stats()
    details = ""
    if DB_PATH.exists():
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT market,
                           COUNT(*) as total,
                           SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                           ROUND(AVG(pnl_pct)*100,1) as avg_pnl
                    FROM signals WHERE resolved=1
                    GROUP BY market ORDER BY total DESC
                """).fetchall()
                for r in rows:
                    wr = r["wins"]/r["total"] if r["total"] > 0 else 0
                    details += (
                        f"\n  {r['market'][:12]:12}"
                        f" {r['wins']}W/{r['losses']}L"
                        f" {wr:.0%}"
                        f" {r['avg_pnl']:+.1f}%"
                    )
        except Exception:
            details = "\n  (pas de donnees)"
    if not details:
        details = "\n  (aucun signal resolu)"
    msg = (
        "=== BILAN APEX-ENGINE ===\n"
        f"Total signaux : {stats['total_signals']}\n"
        f"Resolus       : {stats['resolved']}\n"
        f"Win rate      : {stats['win_rate']:.1%}\n"
        f"P&L cumule    : {stats['pnl_pct']:+.1%}\n"
        f"\nPar marche :{details}\n"
        "========================"
    )
    await bot.send_message(chat_id=chat_id, text=msg)


async def handle_api(bot, chat_id):
    api_key = os.getenv("FOOTBALL_DATA_API_KEY", "") or os.getenv("API_KEY", "")
    results = []
    if api_key:
        for headers, label in [
            ({"x-apisports-key": api_key}, "direct"),
            ({"x-rapidapi-key": api_key,
              "x-rapidapi-host": "v3.football.api-sports.io"}, "rapidapi"),
        ]:
            try:
                resp = requests.get(
                    "https://v3.football.api-sports.io/status",
                    headers=headers, timeout=5
                )
                data = resp.json()
                errors = data.get("errors", {})
                if not errors:
                    r   = data.get("response", {})
                    sub = r.get("subscription", {})
                    req = r.get("requests", {})
                    results.append(
                        f"API-Football [{label}]: OK\n"
                        f"  Plan: {sub.get('plan','?')}\n"
                        f"  Req: {req.get('current','?')}/{req.get('limit_day','?')}/jour"
                    )
                    break
                else:
                    results.append(f"API-Football [{label}]: ERREUR auth")
            except Exception as e:
                results.append(f"API-Football [{label}]: TIMEOUT {e}")
    else:
        results.append("API-Football: CLE NON CONFIGUREE")
    results.append("Telegram: OK")
    msg = "=== ETAT DES APIS ===\n" + "\n".join(results) + "\n===================="
    await bot.send_message(chat_id=chat_id, text=msg)


async def handle_help(bot, chat_id):
    msg = (
        "APEX-ENGINE EPL — COMMANDES\n\n"
        "/start   — Bienvenue\n"
        "/status  — Etat + stats DB\n"
        "/analyse — Analyse immediate\n"
        "/bilan   — P&L par marche\n"
        "/api     — Test connexion APIs\n"
        "/refresh — Vider le cache fixtures\n"
        "/help    — Cette aide\n\n"
        "Analyses auto: 08h/14h/20h UTC\n"
        "Resolver:      23h UTC"
    )
    await bot.send_message(chat_id=chat_id, text=msg)


async def dispatch_command(bot, message, run_pipeline_fn):
    chat_id = message.get("chat", {}).get("id")
    text    = (message.get("text") or "").strip().lower()
    if not chat_id or not text.startswith("/"):
        return
    cmd = text.split()[0]
    logger.info(f"Commande: {cmd} de {chat_id}")
    cmds = {
        "/start":   lambda: handle_start(bot, chat_id),
        "/status":  lambda: handle_status(bot, chat_id),
        "/analyse": lambda: handle_analyse(bot, chat_id, run_pipeline_fn),
        "/bilan":   lambda: handle_bilan(bot, chat_id),
        "/api":     lambda: handle_api(bot, chat_id),
        "/help":    lambda: handle_help(bot, chat_id),
    }
    fn = cmds.get(cmd)
    if fn:
        await fn()
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Commande inconnue: {cmd}\n/help pour la liste."
        )


async def handle_refresh(bot, chat_id):
    """Supprime le cache fixtures pour forcer un re-fetch immediat."""
    import os
    from pathlib import Path
    data_dir  = Path(os.getenv("RENDER_DISK_PATH", "./data"))
    cache_dir = data_dir / "cache"
    deleted   = []
    for f in cache_dir.glob("fixtures_*.json"):
        f.unlink()
        deleted.append(f.name)
    for f in cache_dir.glob("odds_all_epl.json"):
        f.unlink()
        deleted.append(f.name)
    if deleted:
        msg = "Cache supprime:\n" + "\n".join(f"  - {d}" for d in deleted)
        msg += "\n\nLancez /analyse pour recharger."
    else:
        msg = "Aucun cache a supprimer.\nLancez /analyse."
    await bot.send_message(chat_id=chat_id, text=msg)
