"""Hotfixed Render entrypoint.

Use this command on Render:
python app_hotfixed.py
"""

import runtime_hotfix  # patches API-Football odds params, Telegram log noise, and ACL
import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main.main())
