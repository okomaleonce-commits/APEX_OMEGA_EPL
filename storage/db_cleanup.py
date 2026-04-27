"""
Purge les signaux dupliqués dans la DB.
À exécuter une seule fois via : python storage/db_cleanup.py
"""
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(os.getenv("RENDER_DISK_PATH", "./data")) / "apex_epl.db"

def cleanup():
    if not DB_PATH.exists():
        print(f"DB non trouvée: {DB_PATH}")
        return

    with sqlite3.connect(DB_PATH) as conn:
        # Compter les doublons avant
        total_before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

        # Supprimer les doublons : garder seulement le premier (id le plus bas)
        # par combinaison fixture_id + market
        conn.execute("""
            DELETE FROM signals
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM signals
                GROUP BY fixture_id, market
            )
        """)
        conn.commit()

        total_after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        print(f"Signaux avant: {total_before}")
        print(f"Signaux après: {total_after}")
        print(f"Doublons supprimés: {total_before - total_after}")

        # Ajouter l'index unique pour éviter de futurs doublons
        try:
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_signals_fixture_market ON signals(fixture_id, market)
            """)
            conn.commit()
            print("Index UNIQUE créé")
        except Exception as e:
            print(f"Index déjà présent: {e}")

if __name__ == "__main__":
    cleanup()
