"""
test_deploy.py — Production-readiness checks (temporary database, no network).

Covers: configuration validation, tolerant numeric settings, a fresh production database with
NO sample data, /healthz (503 until the bot runtime is attached, then 200), real client IPs
behind a proxy (TRUST_PROXY), the command-center secret not leaking into logs, and the online
database backup script.

Run:  python tests/test_deploy.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = _Path(__file__).resolve().parents[1]
os.environ.pop("SEED_STARTER_ZONES", None)  # production default: no sample zones
os.environ["DASHBOARD_TOKEN"] = "deploy-secret-token-0123456789"
os.environ["TRUST_PROXY"] = "1"

from travel_buddy.core import database
from travel_buddy.safety import ledger, store

TMP_DB = os.path.join(tempfile.mkdtemp(), "deploy_test.db")
for mod in (database, store, ledger):
    mod.DB_PATH = TMP_DB

from travel_buddy import config
from travel_buddy.web import auth, dashboard, runtime

VALID_TOKEN = "123456789:" + "A" * 35


def with_env(overrides: dict, fn):
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def levels(problems, word):
    return [lvl for lvl, msg in problems if word in msg]


def test_config():
    good = {"BOT_TOKEN": VALID_TOKEN, "ADMIN_IDS": "12345", "GROQ_API_KEY": "gsk_real",
            "PUBLIC_BASE_URL": "https://safety.example.org", "SEED_STARTER_ZONES": None,
            "DASHBOARD_TOKEN": "", "WEB_PORT": "8080", "AI_DAILY_LIMIT": "300"}
    assert with_env(good, config.check_config) == [], with_env(good, config.check_config)
    print("[OK] production config passes with no warnings")

    p = with_env({**good, "BOT_TOKEN": "your_telegram_bot_token_here"}, config.check_config)
    assert levels(p, "BOT_TOKEN") == ["error"]
    p = with_env({**good, "BOT_TOKEN": "not-a-token"}, config.check_config)
    assert levels(p, "BOT_TOKEN") == ["error"]
    p = with_env({**good, "ADMIN_IDS": "12,abc"}, config.check_config)
    assert levels(p, "ADMIN_IDS") == ["error"]
    p = with_env({**good, "AI_DAILY_LIMIT": "lots"}, config.check_config)
    assert levels(p, "AI_DAILY_LIMIT") == ["error"]
    print("[OK] bad token / admin IDs / numbers are errors")

    p = with_env({**good, "ADMIN_IDS": ""}, config.check_config)
    assert levels(p, "ADMIN_IDS") == ["warning"]
    p = with_env({**good, "GROQ_API_KEY": "your_groq_api_key_here"}, config.check_config)
    assert levels(p, "GROQ_API_KEY") == ["warning"]
    p = with_env({**good, "PUBLIC_BASE_URL": "http://localhost:8080"}, config.check_config)
    assert levels(p, "PUBLIC_BASE_URL") == ["warning"]
    p = with_env({**good, "PUBLIC_BASE_URL": "http://safety.example.org"}, config.check_config)
    assert levels(p, "https") == ["warning"]
    p = with_env({**good, "SEED_STARTER_ZONES": "1"}, config.check_config)
    assert levels(p, "SEED_STARTER_ZONES") == ["warning"]
    print("[OK] degraded-but-runnable setups are warnings")

    assert with_env({"X_NUM": ""}, lambda: config.env_int("X_NUM", 7)) == 7
    assert with_env({"X_NUM": "abc"}, lambda: config.env_int("X_NUM", 7)) == 7
    assert with_env({"X_NUM": "45"}, lambda: config.env_int("X_NUM", 7)) == 45
    print("[OK] empty / invalid numeric settings fall back to defaults")


async def init_fresh():
    await database.init_db()
    await store.init_safety_db()
    await ledger.ensure_genesis()


def test_fresh_database():
    asyncio.run(init_fresh())
    con = sqlite3.connect(TMP_DB)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("geofence_zones", "users", "incidents", "tourist_ids", "trips", "guardians")}
        blocks = con.execute("SELECT block_type FROM ledger").fetchall()
    finally:
        con.close()
    assert all(v == 0 for v in counts.values()), counts
    assert blocks == [("GENESIS",)], blocks
    print("[OK] fresh production database holds no sample data (only the genesis block)")


def http(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_web():
    cap = LogCapture()
    logging.getLogger("travel_buddy").addHandler(cap)
    logging.getLogger("travel_buddy").setLevel(logging.DEBUG)
    server = dashboard.start_web_server("127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        s, body, _ = http(base, "/healthz")
        data = json.loads(body)
        assert s == 503 and data["database"] is True and data["bot"] is False, (s, data)
        print("[OK] /healthz → 503 while the bot is not running")

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        class Bot:
            username = "DeployTestBot"
        runtime.attach(loop, Bot())
        s, body, _ = http(base, "/healthz")
        assert s == 200 and json.loads(body) == {"ok": True, "web": True, "bot": True, "database": True}, body
        print("[OK] /healthz → 200 when database and bot are up")

        # Rate limits key on the real visitor behind a proxy, not on the proxy's address.
        seen = []
        orig = auth.rate_limited
        auth.rate_limited = lambda key, *a, **kw: seen.append(key) or False
        try:
            req = urllib.request.Request(base + "/api/auth/request", method="POST",
                                         data=json.dumps({"identifier": "@nobody"}).encode(),
                                         headers={"X-Requested-With": "TravelBuddy",
                                                  "Content-Type": "application/json",
                                                  "X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError:
                pass
        finally:
            auth.rate_limited = orig
        assert "req:203.0.113.7" in seen, seen
        print("[OK] TRUST_PROXY: rate limits use the X-Forwarded-For visitor address")

        s, _, headers = http(base, "/")
        assert s == 200 and headers.get("X-Frame-Options") == "DENY"
        print("[OK] landing page served with security headers")

        loop.call_soon_threadsafe(loop.stop)
    finally:
        server.shutdown()
        logging.getLogger("travel_buddy").removeHandler(cap)
    secret = os.environ["DASHBOARD_TOKEN"]
    assert not any(secret in line for line in cap.lines), "command-center secret leaked into logs"
    print("[OK] command-center secret is never written to the logs")


def test_backup():
    out = tempfile.mkdtemp()
    env = {**os.environ, "DATABASE_PATH": TMP_DB, "PYTHONIOENCODING": "utf-8"}
    for _ in range(3):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "backup_db.py"), "--dir", out, "--keep", "2"],
                              env=env, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        import time
        time.sleep(1.1)  # backups are named per second
    files = sorted(_Path(out).glob("*.db"))
    assert len(files) == 2, files
    con = sqlite3.connect(files[-1])
    try:
        assert con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    finally:
        con.close()
    print("[OK] backup script writes verified snapshots and keeps only the newest N")


if __name__ == "__main__":
    print("=" * 68)
    print(">>> DEPLOYMENT READINESS TESTS")
    print("=" * 68)
    test_config()
    test_fresh_database()
    test_web()
    test_backup()
    print("\nALL DEPLOYMENT TESTS PASSED")
