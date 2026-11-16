"""
preflight.py — Pre-deployment check. Run it before going live (and after every config change):

    python scripts/preflight.py            # config + database + Telegram token + web port
    python scripts/preflight.py --offline  # skip the network check (used by the systemd unit)

Exit code 0 = ready (warnings allowed), 1 = something must be fixed first.
"""

import argparse
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from travel_buddy.config import check_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip the Telegram API check")
    args = ap.parse_args()
    errors = warnings = 0

    def report(level: str, msg: str) -> None:
        nonlocal errors, warnings
        icon = {"ok": "✅", "warning": "⚠️ ", "error": "❌"}[level]
        errors += level == "error"
        warnings += level == "warning"
        print(f"{icon} {msg}")

    print("Travel Buddy preflight\n")
    problems = check_config()
    for level, msg in problems:
        report(level, msg)
    if not problems:
        report("ok", "Configuration looks production-ready.")

    # Database: writable, integrity OK
    db_path = os.getenv("DATABASE_PATH") or str(ROOT / "travel_bot.db")
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.commit()
        finally:
            con.close()
        report("ok" if result == "ok" else "error", f"Database {db_path}: integrity {result}")
    except sqlite3.Error as exc:
        report("error", f"Database {db_path} is not usable: {exc}")

    # Web port free (only meaningful when the bot is not already running)
    if os.getenv("WEB_ENABLED", "1") != "0":
        host, port = os.getenv("WEB_HOST", "127.0.0.1"), int(os.getenv("WEB_PORT", "8080") or 8080)
        with socket.socket() as s:
            busy = s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) == 0
        report("warning" if busy else "ok",
               f"Web port {port} is {'already in use (fine if the bot is running)' if busy else 'free'}")

    # Telegram token really works
    token = os.getenv("BOT_TOKEN", "").strip()
    if not args.offline and token and not errors:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as r:
                me = json.load(r)["result"]
            report("ok", f"Telegram token valid: @{me['username']}")
        except urllib.error.HTTPError as exc:
            report("error", f"Telegram rejected the token (HTTP {exc.code}). Check BOT_TOKEN.")
        except OSError as exc:
            report("warning", f"Could not reach Telegram to test the token: {exc}")

    print(f"\n{'READY' if not errors else 'NOT READY'} — {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
