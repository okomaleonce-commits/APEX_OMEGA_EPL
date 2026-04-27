"""
Runtime safety patch loaded automatically by Python at startup.

Goals:
- reduce noisy HTTP client logs that can expose Telegram bot tokens;
- redact Telegram token-like values if a library logs full URLs;
- prevent ACL rule activation from unconfirmed/season-level injury lists.
"""
import logging
import os
import re


class _SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        secrets = [
            os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("BOT_TOKEN", ""),
        ]
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        for secret in secrets:
            if secret:
                message = message.replace(secret, "<REDACTED_TELEGRAM_TOKEN>")

        message = re.sub(
            r"/bot[0-9]+:[A-Za-z0-9_\-]+/",
            "/bot<REDACTED_TELEGRAM_TOKEN>/",
            message,
        )
        record.msg = message
        record.args = ()
        return True


_filter = _SecretRedactionFilter()
logging.getLogger().addFilter(_filter)
for _name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(_name).setLevel(logging.WARNING)
    logging.getLogger(_name).addFilter(_filter)


def _patch_acl_rule():
    try:
        import rules.rule_engine as rule_engine
    except Exception:
        return

    original_r9_acl = getattr(rule_engine, "r9_acl", None)
    if not original_r9_acl:
        return

    def safe_r9_acl(lineup_data, probs, is_home_team):
        if isinstance(lineup_data, dict):
            allow_unconfirmed = os.getenv("ALLOW_UNCONFIRMED_ACL", "true").lower() in {"1", "true", "yes", "on"}
            if lineup_data.get("lineup_confirmed") is False and not allow_unconfirmed:
                return probs, 1.0, None, False

            injured_players = lineup_data.get("injured_players") or []
            max_players = int(os.getenv("ACL_MAX_INJURY_LIST_SIZE", "8"))
            if len(injured_players) > max_players and not allow_unconfirmed:
                return probs, 1.0, None, False

        return original_r9_acl(lineup_data, probs, is_home_team)

    rule_engine.r9_acl = safe_r9_acl


_patch_acl_rule()
