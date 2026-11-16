"""
dashboard.py — Web Command Center & public verification portal (Python stdlib only).

Routes
  /                         Landing page (links to public tools)
  /command?token=…          🔒 Live command-center map for the safety desk (auto-refresh)
  /api/overview?token=…     🔒 JSON feed used by the map
  /verify/<TID>             🌐 Public Digital Tourist ID verification (target of the QR code)
  /ledger                   🌐 Public blockchain ledger explorer (hashes only, no personal data)
  /api/ledger               🌐 JSON ledger summary

Runs in a background thread next to the bot; reads SQLite directly (read-only).
"""

import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from travel_buddy.core import database
from travel_buddy.safety.incidents import is_admin
from travel_buddy.web import auth, pages, portal_api, runtime
from travel_buddy.safety.ledger import compute_identity_hash, verify_blocks
from travel_buddy.safety.geofence import RISK_ORDER
from travel_buddy.safety.risk_intel import HOTSPOT_DAYS, cached_hotspots

logger = logging.getLogger(__name__)

def _load_or_create_token() -> str:
    """DASHBOARD_TOKEN from .env, else a random token generated ONCE and kept in the database,
    so command-center links keep working across restarts. Delete it from app_settings to rotate."""
    if os.getenv("DASHBOARD_TOKEN"):
        return os.environ["DASHBOARD_TOKEN"]
    try:
        con = sqlite3.connect(database.DB_PATH, timeout=5)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = con.execute("SELECT value FROM app_settings WHERE key = 'dashboard_token'").fetchone()
            if row:
                return row[0]
            token = secrets.token_urlsafe(16)
            con.execute("INSERT INTO app_settings (key, value) VALUES ('dashboard_token', ?)", (token,))
            con.commit()
            return token
        finally:
            con.close()
    except sqlite3.Error:
        return secrets.token_urlsafe(16)


_token = _load_or_create_token()


def dashboard_token() -> str:
    return _token


_bound_port: int | None = None  # the port actually in use (may differ from WEB_PORT if it was busy)


def public_base_url() -> str:
    port = _bound_port or int(os.getenv("WEB_PORT", "8080"))
    return os.getenv("PUBLIC_BASE_URL", f"http://localhost:{port}").rstrip("/")


# ─────────────────────────────────────────────
#  Data access (sync, read-only)
# ─────────────────────────────────────────────

def _q(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(database.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _names() -> dict[int, dict]:
    out = {r["telegram_id"]: {"name": r["username"] or f"User {r['telegram_id']}", "tid": None}
           for r in _q("SELECT telegram_id, username FROM users")}
    for r in _q("SELECT telegram_id, full_name, tourist_id FROM tourist_ids WHERE status = 'active'"):
        out[r["telegram_id"]] = {"name": r["full_name"], "tid": r["tourist_id"]}
    return out


def build_overview() -> dict:
    now = time.time()
    names = _names()
    zones = _q("SELECT * FROM geofence_zones WHERE active = 1")
    zone_by_id = {z["zone_id"]: z for z in zones}

    tourists = []
    for s in _q("SELECT * FROM safety_state WHERE last_lat IS NOT NULL AND last_seen_ts >= ?", (now - 86400,)):
        inside = json.loads(s.get("zone_state") or "{}").get("inside", [])
        risk = max((zone_by_id[z]["risk_level"] for z in inside if z in zone_by_id),
                   key=lambda r: RISK_ORDER[r], default="safe")
        who = names.get(s["telegram_id"], {"name": f"User {s['telegram_id']}", "tid": None})
        tourists.append({
            "id": s["telegram_id"], "name": who["name"], "tid": who["tid"],
            "lat": s["last_lat"], "lng": s["last_lng"],
            "minutes_ago": int((now - s["last_seen_ts"]) // 60),
            "zone_risk": risk, "checkin_pending": bool(s["checkin_pending_ts"]),
        })

    incidents = _q("SELECT * FROM incidents ORDER BY incident_id DESC LIMIT 100")
    for i in incidents:
        who = names.get(i["telegram_id"], {"name": f"User {i['telegram_id']}", "tid": None})
        i["name"], i["tid"] = who["name"], who["tid"]

    since = (datetime.now(timezone.utc) - timedelta(days=HOTSPOT_DAYS)).isoformat()
    hotspots = cached_hotspots(_q(
        "SELECT * FROM incidents WHERE lat IS NOT NULL AND created_at >= ? "
        "AND incident_type NOT IN ('no_response', 'geofence_breach')", (since,)))

    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    open_inc = [i for i in incidents if i["status"] != "resolved"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "users": _q("SELECT COUNT(*) AS n FROM users")[0]["n"],
            "digital_ids": _q("SELECT COUNT(*) AS n FROM tourist_ids WHERE status='active'")[0]["n"],
            "active_trips": _q("SELECT COUNT(*) AS n FROM trips WHERE status='active'")[0]["n"],
            "live_tourists": sum(1 for t in tourists if t["minutes_ago"] <= 60),
            "open_incidents": len(open_inc),
            "critical": sum(1 for i in open_inc if i["severity"] == "critical"),
            "pending_checkins": sum(1 for t in tourists if t["checkin_pending"]),
            "in_risk_zones": sum(1 for t in tourists if RISK_ORDER[t["zone_risk"]] >= RISK_ORDER["high"]),
            "hotspots": len([h for h in hotspots if h["risk_level"] != "safe"]),
        },
        "ledger": {"valid": chain["valid"], "length": chain["length"], "broken_at": chain["broken_at"],
                   "head": chain.get("head")},
        "tourists": tourists,
        "incidents": incidents,
        "zones": zones,
        "hotspots": hotspots,
    }


def verify_tid_sync(tid: str) -> dict:
    tid = tid.upper()
    res = {"tourist_id": tid, "found": False, "valid": False, "reasons": []}
    rows = _q("SELECT * FROM tourist_ids WHERE tourist_id = ?", (tid,))
    if not rows:
        res["reasons"].append("No such Tourist ID on record.")
        return res
    rec = rows[0]
    res.update(found=True, record=rec)
    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    res["chain"] = chain
    if not chain["valid"]:
        res["reasons"].append(f"Ledger integrity broken at block #{chain['broken_at']}.")
    block = _q("SELECT * FROM ledger WHERE hash = ?", (rec["block_hash"],))
    if not block:
        res["reasons"].append("Sealing block missing from ledger.")
    else:
        res["block_index"] = block[0]["block_index"]
        if json.loads(block[0]["payload"]).get("identity_hash") != compute_identity_hash(rec):
            res["reasons"].append("Record altered since issue (hash mismatch).")
    if rec["status"] != "active":
        res["reasons"].append("ID has been revoked.")
    if datetime.fromisoformat(rec["valid_until"]) < datetime.now(timezone.utc):
        res["reasons"].append("ID validity period has expired.")
    res["valid"] = not res["reasons"]
    return res


# ─────────────────────────────────────────────
#  Views & static assets
# ─────────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2"}


def public_stats() -> dict:
    """Aggregate, non-personal numbers for the public landing page."""
    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    return {
        "ids": _q("SELECT COUNT(*) AS n FROM tourist_ids WHERE status='active'")[0]["n"],
        "blocks": chain["length"],
        "chain_valid": chain["valid"],
        "zones": _q("SELECT COUNT(*) AS n FROM geofence_zones WHERE active=1")[0]["n"],
        "resolved": _q("SELECT COUNT(*) AS n FROM incidents WHERE status='resolved'")[0]["n"],
    }


def landing_html(session: dict | None = None) -> str:
    return pages.landing_html(public_stats(), signed_in=bool(session))


def verify_html(res: dict) -> str:
    return pages.verify_html(res)


def ledger_html() -> str:
    blocks = _q("SELECT * FROM ledger ORDER BY block_index DESC LIMIT 60")
    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    return pages.ledger_html(blocks, chain)


def command_html(token: str) -> str:
    return pages.command_html(token)


# ─────────────────────────────────────────────
#  Server
# ─────────────────────────────────────────────

# Browsers close connections all the time (tab closed, page changed mid-poll): not an error.
CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class Handler(BaseHTTPRequestHandler):
    server_version = "TravelBuddySafety/1.0"

    def log_message(self, fmt, *args):  # route to our logger, quietly
        logger.debug("web %s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              cache: str = "no-store", headers: list | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers or []:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except CLIENT_GONE:
            logger.debug("Client closed the connection before the response was sent: %s", self.path)

    def _redirect(self, location: str, headers: list | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in headers or []:
            self.send_header(k, v)
        self.end_headers()

    def _authorised(self, qs: dict) -> bool:
        given = (qs.get("token") or [""])[0] or self.headers.get("X-TB-Token", "")
        return bool(given) and hmac.compare_digest(given, _token)

    # ── sessions ──
    def _cookie_token(self) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = jar.get(auth.COOKIE)
        return morsel.value if morsel else None

    def _session(self) -> dict | None:
        return auth.get_session(self._cookie_token())

    def _api(self, method: str, path: str, body: dict) -> None:
        session = self._session()
        resp = portal_api.handle(method, path, body, session, self._authorised({}), self.client_address[0])
        if isinstance(resp.body, dict):
            return self._send(resp.status, json.dumps(resp.body, default=str), "application/json",
                              headers=resp.headers)
        return self._send(resp.status, resp.body, resp.ctype, headers=resp.headers)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            if not path.startswith("/api/"):
                return self._send(404, "Not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > (9 * 1024 * 1024 if path == "/api/me/evidence" else 64 * 1024):
                return self._send(413, json.dumps({"ok": False, "error": "Request too large."}), "application/json")
            # Browsers can't send this header cross-site without CORS approval → blocks form-based CSRF.
            if self.headers.get("X-Requested-With") != "TravelBuddy":
                return self._send(403, json.dumps({"ok": False, "error": "Forbidden."}), "application/json")
            try:
                body = portal_api.parse_json(self.rfile.read(length))
            except portal_api.ApiError as exc:
                return self._send(exc.status, json.dumps({"ok": False, "error": exc.message}), "application/json")
            session = self._session()
            body["_csrf_ok"] = bool(session) and hmac.compare_digest(
                self.headers.get("X-CSRF", ""), session["csrf"])
            if path == "/api/auth/logout":
                auth.delete_session(self._cookie_token())
            return self._api("POST", path, body)
        except CLIENT_GONE:
            logger.debug("Client went away during %s", self.path)
        except Exception:
            logger.exception("Web POST failed: %s", self.path)
            return self._send(500, json.dumps({"ok": False, "error": "Server error, please try again."}),
                              "application/json")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._send(200, landing_html(self._session()))
            if path == "/login":
                if self._session():
                    return self._redirect("/app")
                return self._send(200, pages.login_html(runtime.bot_username()))
            if path == "/app":
                session = self._session()
                if not session:
                    return self._redirect("/login")
                return self._send(200, pages.app_html())
            if path == "/auth/magic":
                uid = auth.consume_magic_token((qs.get("t") or [""])[0])
                if not uid:
                    return self._send(400, pages.error_html(
                        400, "Link expired", "This sign-in link was already used or has expired. "
                        "Send /login to the bot for a new one.", "⏳"))
                token = auth.create_session(uid)
                return self._redirect("/app", [("Set-Cookie", portal_api.session_cookie(token))])
            if path.startswith("/api/") and path not in ("/api/ledger", "/api/overview"):
                return self._api("GET", path, {})
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/verify/"):
                return self._send(200, verify_html(verify_tid_sync(path.split("/verify/", 1)[1][:32])))
            if path == "/ledger":
                return self._send(200, ledger_html())
            if path == "/api/ledger":
                chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
                return self._send(200, json.dumps(chain), "application/json")
            if path in ("/command", "/api/overview"):
                session = self._session()
                admin_session = bool(session) and is_admin(session["uid"])
                if not (self._authorised(qs) or admin_session):
                    return self._send(403, pages.error_html(
                        403, "Access denied", "The command center is for the safety desk. Sign in with an "
                        "admin Telegram account, or use the private link from the bot's /web command.", "🔒"))
                if path == "/command":
                    return self._send(200, command_html(_token))
                return self._send(200, json.dumps(build_overview(), default=str), "application/json")
            return self._send(404, pages.error_html(404, "Page not found", "This page drifted off the map.", "🧭"))
        except CLIENT_GONE:
            logger.debug("Client went away during %s", self.path)
        except Exception:
            logger.exception("Web request failed: %s", self.path)
            return self._send(500, pages.error_html(500, "Something went wrong", "Please try again in a moment.", "⚠️"))


    def _static(self, name: str):
        # Only serve plain file names that exist in STATIC_DIR (no sub-paths, no traversal).
        if "/" in name or "\\" in name or name.startswith(".") or not (STATIC_DIR / name).is_file():
            return self._send(404, pages.error_html(404, "Page not found", "This page drifted off the map.", "🧭"))
        ctype = STATIC_TYPES.get(Path(name).suffix.lower())
        if not ctype:
            return self._send(404, "Not found", "text/plain")
        return self._send(200, (STATIC_DIR / name).read_bytes(), ctype, cache="public, max-age=300")


class PortalServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that treats client disconnects as normal and logs real errors properly."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            logger.debug("Client %s disconnected early", client_address)
            return
        logger.exception("Unhandled web server error from %s", client_address)


_server: ThreadingHTTPServer | None = None


PORT_FALLBACK_TRIES = 20


def start_web_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer | None:
    """Start the portal; if the port is busy or blocked, try the next ones (8080 → 8081 → …)."""
    global _server, _bound_port
    host = host or os.getenv("WEB_HOST", "127.0.0.1")
    first = port if port is not None else int(os.getenv("WEB_PORT", "8080"))
    candidates = [first] if first == 0 else range(first, first + PORT_FALLBACK_TRIES)
    last_exc = None
    for p in candidates:
        try:
            _server = PortalServer((host, p), Handler)
            break
        except OSError as exc:
            last_exc = exc
    else:
        logger.warning("Web command center not started (%s:%s-%s): %s",
                       host, first, first + PORT_FALLBACK_TRIES - 1, last_exc)
        return None
    _bound_port = _server.server_address[1]
    if first and _bound_port != first:
        logger.warning("Port %s is in use by another program; web portal moved to port %s.", first, _bound_port)
    threading.Thread(target=_server.serve_forever, name="web-dashboard", daemon=True).start()
    logger.info("Web command center: %s/command?token=%s", public_base_url(), _token)
    return _server


def stop_web_server() -> None:
    if _server:
        _server.shutdown()
