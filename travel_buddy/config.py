"""
config.py — Start-up configuration checks shared by main.py and scripts/preflight.py.

check_config() returns a list of (level, message) where level is "error" (the bot cannot run)
or "warning" (it runs, but a feature is degraded or the setup is not production-ready).
"""

import os
import re

PLACEHOLDERS = {"", "your_telegram_bot_token_here", "gsk_your_groq_api_key_here", "your_groq_api_key_here"}
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")


def env_int(name: str, default: int) -> int:
    """Whole-number setting; empty or invalid values fall back to the default (check_config reports them)."""
    try:
        return int(float(os.getenv(name, "") or default))
    except ValueError:
        return default


def _int_env(name: str, default: int, problems: list, minimum: int = 0) -> None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return
    try:
        if int(raw) < minimum:
            raise ValueError
    except ValueError:
        problems.append(("error", f"{name}={raw!r} must be a whole number ≥ {minimum} (default {default})."))


def check_config() -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []

    token = os.getenv("BOT_TOKEN", "").strip()
    if token in PLACEHOLDERS:
        problems.append(("error", "BOT_TOKEN is not set. Put your token from @BotFather in .env."))
    elif not TOKEN_RE.match(token):
        problems.append(("error", "BOT_TOKEN does not look like a Telegram token (expected 123456:ABC…)."))

    admins = [x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    if not admins:
        problems.append(("warning", "ADMIN_IDS is empty: nobody receives SOS alerts or can open the command "
                                    "center. Send /dashboard to the bot to see your ID, then add it."))
    elif not all(a.isdigit() for a in admins):
        problems.append(("error", "ADMIN_IDS must be numeric Telegram IDs separated by commas."))

    if os.getenv("GROQ_API_KEY", "").strip() in PLACEHOLDERS:
        problems.append(("warning", "GROQ_API_KEY is not set: AI features run in their free offline mode."))

    if os.getenv("WEB_ENABLED", "1") != "0":
        base = os.getenv("PUBLIC_BASE_URL", "").strip()
        if not base or "localhost" in base or "127.0.0.1" in base:
            problems.append(("warning", "PUBLIC_BASE_URL is a local address: QR codes, magic links and the "
                                        "portal only work on this computer. Set your public https:// URL."))
        elif not base.startswith("https://"):
            problems.append(("warning", "PUBLIC_BASE_URL is not https://: browsers block location on plain "
                                        "http and session cookies are not marked Secure."))
        token_len = len(os.getenv("DASHBOARD_TOKEN", ""))
        if 0 < token_len < 16:
            problems.append(("warning", "DASHBOARD_TOKEN is shorter than 16 characters; use a long random value "
                                        "or leave it empty to auto-generate one."))
        _int_env("WEB_PORT", 8080, problems, minimum=0)

    for name, default in [("SAFETY_INACTIVITY_MIN", 30), ("SAFETY_ESCALATE_MIN", 15),
                          ("AI_DAILY_LIMIT", 300), ("WEATHER_DAILY_LIMIT", 5000),
                          ("ROUTING_DAILY_LIMIT", 500), ("PLACES_DAILY_LIMIT", 2000)]:
        _int_env(name, default, problems)

    if os.getenv("SEED_STARTER_ZONES", "0") == "1":
        problems.append(("warning", "SEED_STARTER_ZONES=1 loads sample geo-fence zones into an empty database. "
                                    "Turn it off for production and add real zones with /addzone."))
    return problems
