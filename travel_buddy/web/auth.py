"""
auth.py — Passwordless web login using the user's Telegram account.

Two ways to sign in, both proving the person controls the Telegram account:
  1. Login code: enter your Telegram @username, Telegram ID or Tourist ID on /login →
     the bot sends a 6-digit code to that Telegram chat → enter it (10 min, 5 attempts).
  2. Magic link: send /login to the bot → it replies with a one-tap link (15 min, single use).

Sessions are random 256-bit tokens kept in an HttpOnly, SameSite=Lax cookie; only their
SHA-256 hashes are stored. Every state-changing request also needs the session's CSRF token.
Nothing here stores a password.
"""

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time

from travel_buddy.core import database

logger = logging.getLogger(__name__)

SESSION_TTL_S = 7 * 86400
CODE_TTL_S = 10 * 60
CODE_RESEND_S = 45
CODE_MAX_ATTEMPTS = 5
MAGIC_TTL_S = 15 * 60
COOKIE = "tb_session"


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(database.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, csrf TEXT NOT NULL,
            created_ts REAL NOT NULL, expires_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS web_login_codes (
            telegram_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL, expires_ts REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, sent_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS web_magic_links (
            token_hash TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, expires_ts REAL NOT NULL);
    """)
    return con


# ─────────────────────────────────────────────
#  Identify the Telegram user
# ─────────────────────────────────────────────

def find_user(identifier: str) -> int | None:
    """@username, numeric Telegram ID or Tourist ID → Telegram ID of someone who started the bot."""
    ident = (identifier or "").strip().lstrip("@")
    if not ident or len(ident) > 64:
        return None
    con = _con()
    try:
        if ident.isdigit():
            row = con.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (int(ident),)).fetchone()
        elif ident.upper().startswith("TID-"):
            row = con.execute("SELECT telegram_id FROM tourist_ids WHERE tourist_id = ?", (ident.upper(),)).fetchone()
        else:
            row = con.execute("SELECT telegram_id FROM users WHERE lower(username) = lower(?)", (ident,)).fetchone()
        return row["telegram_id"] if row else None
    finally:
        con.close()


# ─────────────────────────────────────────────
#  Login codes
# ─────────────────────────────────────────────

def issue_code(uid: int) -> str | None:
    """Create a fresh 6-digit code for uid, or None if one was sent moments ago."""
    con = _con()
    try:
        row = con.execute("SELECT sent_ts FROM web_login_codes WHERE telegram_id = ?", (uid,)).fetchone()
        if row and time.time() - row["sent_ts"] < CODE_RESEND_S:
            return None
        code = f"{secrets.randbelow(10**6):06d}"
        con.execute("INSERT OR REPLACE INTO web_login_codes (telegram_id, code_hash, expires_ts, attempts, sent_ts) "
                    "VALUES (?, ?, ?, 0, ?)", (uid, _h(f"{uid}:{code}"), time.time() + CODE_TTL_S, time.time()))
        con.commit()
        return code
    finally:
        con.close()


def verify_code(uid: int, code: str) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    con = _con()
    try:
        row = con.execute("SELECT * FROM web_login_codes WHERE telegram_id = ?", (uid,)).fetchone()
        if not row or row["expires_ts"] < time.time() or row["attempts"] >= CODE_MAX_ATTEMPTS:
            return False
        if hmac.compare_digest(row["code_hash"], _h(f"{uid}:{code}")):
            con.execute("DELETE FROM web_login_codes WHERE telegram_id = ?", (uid,))
            con.commit()
            return True
        con.execute("UPDATE web_login_codes SET attempts = attempts + 1 WHERE telegram_id = ?", (uid,))
        con.commit()
        return False
    finally:
        con.close()


# ─────────────────────────────────────────────
#  Magic links (/login in the bot)
# ─────────────────────────────────────────────

def issue_magic_token(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    con = _con()
    try:
        con.execute("DELETE FROM web_magic_links WHERE expires_ts < ?", (time.time(),))
        con.execute("INSERT INTO web_magic_links (token_hash, telegram_id, expires_ts) VALUES (?, ?, ?)",
                    (_h(token), uid, time.time() + MAGIC_TTL_S))
        con.commit()
        return token
    finally:
        con.close()


def consume_magic_token(token: str) -> int | None:
    con = _con()
    try:
        row = con.execute("SELECT * FROM web_magic_links WHERE token_hash = ?", (_h(token or ""),)).fetchone()
        if not row:
            return None
        con.execute("DELETE FROM web_magic_links WHERE token_hash = ?", (row["token_hash"],))
        con.commit()
        return row["telegram_id"] if row["expires_ts"] >= time.time() else None
    finally:
        con.close()


# ─────────────────────────────────────────────
#  Sessions
# ─────────────────────────────────────────────

def create_session(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    con = _con()
    try:
        con.execute("DELETE FROM web_sessions WHERE expires_ts < ?", (time.time(),))
        con.execute("INSERT INTO web_sessions (token_hash, telegram_id, csrf, created_ts, expires_ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (_h(token), uid, secrets.token_urlsafe(24), time.time(), time.time() + SESSION_TTL_S))
        con.commit()
        logger.info("Web session created for %s", uid)
        return token
    finally:
        con.close()


def get_session(token: str | None) -> dict | None:
    if not token:
        return None
    con = _con()
    try:
        row = con.execute("SELECT * FROM web_sessions WHERE token_hash = ?", (_h(token),)).fetchone()
        if not row or row["expires_ts"] < time.time():
            return None
        return {"uid": row["telegram_id"], "csrf": row["csrf"]}
    finally:
        con.close()


def delete_session(token: str | None) -> None:
    if not token:
        return
    con = _con()
    try:
        con.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_h(token),))
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────
#  Simple per-client rate limit for login attempts
# ─────────────────────────────────────────────

_hits: dict[str, list[float]] = {}


def rate_limited(client: str, limit: int = 10, window_s: int = 600) -> bool:
    now = time.time()
    hits = [t for t in _hits.get(client, []) if now - t < window_s]
    hits.append(now)
    _hits[client] = hits
    return len(hits) > limit
