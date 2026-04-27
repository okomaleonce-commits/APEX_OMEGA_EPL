import logging
import os
import requests

# Stop http client INFO logs from printing Telegram URLs with bot token.
for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(name).setLevel(logging.WARNING)

_original_requests_get = requests.get


def patched_requests_get(url, *args, **kwargs):
    params = kwargs.get("params")
    if isinstance(url, str) and "/odds" in url and isinstance(params, dict):
        if "next" in params:
            params = dict(params)
            params.pop("next", None)
            params.setdefault("page", 1)
            kwargs["params"] = params
    return _original_requests_get(url, *args, **kwargs)


requests.get = patched_requests_get


def apply_rule_engine_patch():
    try:
        import rules.rule_engine as rule_engine
    except Exception:
        return

    original = getattr(rule_engine, "r9_acl", None)
    if not original or getattr(original, "_apex_safe_patch", False):
        return

    def safe_r9_acl(lineup_data, probs, is_home_team):
        allow_unconfirmed = os.getenv("ALLOW_UNCONFIRMED_ACL", "false").lower() in {"1", "true", "yes", "on"}
        if isinstance(lineup_data, dict) and not allow_unconfirmed:
            if lineup_data.get("lineup_confirmed") is False:
                return probs, 1.0, None, False
            injured = lineup_data.get("injured_players") or []
            if len(injured) > int(os.getenv("ACL_MAX_INJURY_LIST_SIZE", "8")):
                return probs, 1.0, None, False
        return original(lineup_data, probs, is_home_team)

    safe_r9_acl._apex_safe_patch = True
    rule_engine.r9_acl = safe_r9_acl
