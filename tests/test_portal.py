"""
test_portal.py — End-to-end tests for the tourist / guardian web portal (real HTTP server,
temporary database, fake Telegram bot, no external network).

Covers: code + magic-link sign-in, CSRF protection, overview, location → geo-fence alerts,
Digital ID + QR, reports, SOS (guardian + admin alerted), guardian family view, acknowledge /
resolve with Telegram notification, permissions, trips with live journey alerts, timer,
settings, E-FIR, officer actions from the command center, logout.

Run:  python tests/test_portal.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["ADMIN_IDS"] = "9001"
os.environ["GROQ_API_KEY"] = ""
os.environ["DASHBOARD_TOKEN"] = "desk-token-xyz"

from travel_buddy.core import database
from travel_buddy.safety import ledger, store

TMP_DB = os.path.join(tempfile.mkdtemp(), "portal_test.db")
for mod in (database, store, ledger):
    mod.DB_PATH = TMP_DB

from travel_buddy.bot import intel_handlers
from travel_buddy.safety import actions
from travel_buddy.web import dashboard, portal_api, runtime

TOURIST, GUARDIAN, STRANGER, ADMIN = 1001, 2002, 3003, 9001


class FakeBot:
    username = "TravelBuddyTestBot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))

    async def send_location(self, *a, **kw):
        pass

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        """Behaves like Telegram: returns a message whose largest photo has a file_id."""
        from types import SimpleNamespace
        self.sent.append((chat_id, f"[photo] {caption or ''}"))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id=f"tg-photo-{len(self.sent)}")])


# ── no external network in tests ──
async def fake_reverse(lat, lng):
    return {"display_name": "Ramganj, Jaipur, Rajasthan, India", "country_code": "in"}


async def fake_weather(lat, lng):
    return {"summary": "clear, 24°C", "temperature": 24, "hazards": [], "worst": "none",
            "timezone": "Asia/Kolkata", "utc_offset_seconds": 19800}


async def fake_services(lat, lng, radius_m=5000, per_type=2):
    return [{"type": "police", "name": "Manak Chowk PS", "phone": None, "lat": lat + .001, "lng": lng, "distance_m": 110.0}]


async def fake_geocode(q):
    return (26.9196, 75.7878, "Jaipur Junction, Jaipur, Rajasthan, India")


intel_handlers.reverse_geocode = fake_reverse
intel_handlers.get_weather_risk = fake_weather
portal_api.reverse_geocode = fake_reverse
portal_api.find_nearby_services = fake_services
portal_api.geocode_location = fake_geocode
actions.reverse_geocode = fake_reverse

BOT = FakeBot()
LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()
runtime.attach(LOOP, BOT)


def ok(msg):
    print(f"[OK] {msg}")


class Client:
    def __init__(self, base):
        self.base, self.jar, self.csrf = base, CookieJar(), ""
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def req(self, method, path, data=None, headers=None, csrf=True):
        h = {"X-Requested-With": "TravelBuddy", "Content-Type": "application/json"}
        if csrf and self.csrf:
            h["X-CSRF"] = self.csrf
        h.update(headers or {})
        body = json.dumps(data or {}).encode() if method == "POST" else None
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with self.opener.open(r, timeout=30) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def json(self, method, path, data=None, **kw):
        status, _, raw = self.req(method, path, data, **kw)
        return status, json.loads(raw or b"{}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def last_code_for(uid):
    for chat, text in reversed(BOT.sent):
        if chat == uid and "login code" in text:
            return re.search(r"<code>(\d{6})</code>", text).group(1)
    raise AssertionError("no code sent")


def sign_in(c, identifier, uid):
    status, body = c.json("POST", "/api/auth/request", {"identifier": identifier})
    assert status == 200, body
    status, body = c.json("POST", "/api/auth/verify", {"identifier": identifier, "code": last_code_for(uid)})
    assert status == 200 and body["ok"], body
    status, me = c.json("GET", "/api/me")
    c.csrf = me["csrf"]
    return me


async def seed():
    await database.init_db()
    await store.init_safety_db()
    await ledger.ensure_genesis()
    for uid, name in [(TOURIST, "asha"), (GUARDIAN, "ravi"), (STRANGER, "eve"), (ADMIN, "officer")]:
        await database.upsert_user(uid, name)
    await store.create_guardian_invite(TOURIST, "Ravi", "invite1")
    await store.link_guardian("invite1", GUARDIAN)


def main():
    runtime.run(seed())
    server = dashboard.start_web_server("127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        run_tests(base)
    finally:
        server.shutdown()


def run_tests(base):
    print("=" * 68)
    print(">>> WEB PORTAL TESTS")
    print("=" * 68)
    t = Client(base)

    # ── pages & redirects ──
    s, h, raw = t.req("GET", "/login")
    assert s == 200 and b"Sign in to your portal" in raw
    s, h, _ = t.req("GET", "/app")
    assert s == 303 and h["Location"] == "/login"
    ok("Login page served; portal redirects to /login when signed out")

    # ── sign-in with a code sent via Telegram ──
    s, body = t.json("POST", "/api/auth/request", {"identifier": "@asha"}, headers={"X-Requested-With": ""})
    assert s == 403, "requests without the anti-CSRF header must be rejected"
    s, body = t.json("POST", "/api/auth/request", {"identifier": "nobody_here"})
    assert s == 200 and "If that account" in body["message"]
    s, body = t.json("POST", "/api/auth/request", {"identifier": "@asha"})
    code = last_code_for(TOURIST)
    s, body = t.json("POST", "/api/auth/verify", {"identifier": "@asha", "code": "000000" if code != "000000" else "111111"})
    assert s == 401
    me = sign_in(t, "@asha", TOURIST)
    assert me["uid"] == TOURIST and not me["is_admin"] and me["guarding"] == 0
    s, h, _ = t.req("GET", "/app")
    assert s == 200
    ok("Sign-in: code delivered to Telegram, wrong code refused, session cookie works (no user enumeration)")

    # ── CSRF ──
    s, body = t.json("POST", "/api/me/location", {"lat": 26.9239, "lng": 75.8267}, csrf=False)
    assert s == 403
    ok("State-changing request without CSRF token rejected")

    # ── location → geo-fence alert ──
    s, body = t.json("POST", "/api/me/location", {"lat": 26.9239, "lng": 75.8267})
    assert s == 200 and any("Jaipur Old City" in n for n in body["notes"]), body
    s, ov = t.json("GET", "/api/me/overview")
    assert ov["zones_inside"][0]["risk"] == "caution" and ov["score"]["score"] < 100 and ov["place"].startswith("Ramganj")
    ok(f"Location → geo-fence alert; overview score {ov['score']['score']} ({ov['score']['label']})")

    # ── Digital ID ──
    s, body = t.json("POST", "/api/me/id", {"full_name": "Asha Verma", "nationality": "Indian", "doc_type": "Passport",
                                            "doc_number": "Z1234567", "emergency_name": "Ravi", "emergency_phone": "+91 98765 43210",
                                            "days": 7})
    assert s == 200 and body["digital_id"]["tourist_id"].startswith("TID-") and "Z1234567" not in json.dumps(body)
    tid = body["digital_id"]["tourist_id"]
    s, h, png = t.req("GET", "/api/me/id/qr.png")
    assert s == 200 and png[:4] == b"\x89PNG"
    s, body = t.json("POST", "/api/me/id/verify")
    assert body["valid"]
    s, body = t.json("POST", "/api/me/id", {"full_name": "Again"})
    assert s == 409
    ok(f"Digital ID issued ({tid}), QR served, verified; duplicate refused")

    # ── report & SOS ──
    BOT.sent.clear()
    s, body = t.json("POST", "/api/me/report", {"type": "theft", "description": "Wallet snatched at the market", "use_location": True})
    assert s == 200 and body["incident"]["severity"] == "medium" and body["incident"]["lat"] is not None
    assert any(chat == GUARDIAN for chat, _ in BOT.sent)
    BOT.sent.clear()
    s, body = t.json("POST", "/api/me/sos", {"lat": 26.9240, "lng": 75.8268})
    assert s == 200 and body["incident"]["severity"] == "critical" and body["delivered"] == 2
    assert {c for c, _ in BOT.sent} >= {GUARDIAN, ADMIN} and body["emergency_numbers"]["All emergencies"] == "112"
    sos_id = body["incident"]["id"]
    ok("Report (guardian alerted) and SOS (guardian + safety desk alerted, numbers + nearby help returned)")

    # ── guardian via magic link ──
    g = Client(base)
    token = runtime.run(asyncio.to_thread(__import__("travel_buddy.web.auth", fromlist=["x"]).issue_magic_token, GUARDIAN))
    s, h, _ = g.req("GET", f"/auth/magic?t={token}")
    assert s == 303 and h["Location"] == "/app"
    s, me = g.json("GET", "/api/me")
    g.csrf = me["csrf"]
    assert me["uid"] == GUARDIAN and me["guarding"] == 1
    s, h, _ = g.req("GET", f"/auth/magic?t={token}")
    assert s == 400, "magic links are single-use"
    s, fam = g.json("GET", "/api/family")
    person = fam["people"][0]
    assert person["id"] == TOURIST and person["tourist_id"] == tid and any(i["id"] == sos_id for i in person["incidents"])
    ok("Guardian signs in with single-use magic link and sees the tourist's live status + SOS")

    BOT.sent.clear()
    s, body = g.json("POST", f"/api/incident/{sos_id}/ack")
    assert s == 200 and body["incident"]["status"] == "acknowledged"
    assert any(c == TOURIST and "has seen your alert" in txt for c, txt in BOT.sent)
    s, h, efir = g.req("GET", f"/api/incident/{sos_id}/efir")
    assert s == 200 and b"E-FIR" in efir and b"Asha Verma" in efir
    ok("Guardian acknowledges SOS (tourist notified on Telegram) and downloads the E-FIR")

    # ── permissions ──
    x = Client(base)
    sign_in(x, "eve", STRANGER)
    s, _ = x.json("POST", f"/api/incident/{sos_id}/resolve")
    assert s == 403
    s, _, _ = x.req("GET", f"/api/incident/{sos_id}/efir")
    assert s == 404
    s, fam = x.json("GET", "/api/family")
    assert fam["people"] == []
    s, _, _ = x.req("GET", "/command")
    assert s == 403
    ok("A stranger can't see, resolve or download someone else's incidents, nor open the command center")

    # ── I'm safe ──
    s, body = t.json("POST", "/api/me/safe")
    assert s == 200 and sos_id in body["resolved"]
    ok("'I'm safe' closes the SOS and informs responders")

    # ── trip with live journey alerts ──
    s, body = t.json("POST", "/api/me/trip", {"destination": "Jaipur Junction", "mode": "train"})
    assert s == 200
    notes = []
    for dlat in (0.045, 0.006, 0.0005):
        s, body = t.json("POST", "/api/me/location", {"lat": 26.9196 + dlat, "lng": 75.7878})
        notes += body["notes"]
    joined = " ".join(notes)
    assert "tracking started" in joined and "APPROACHING YOUR STOP" in joined and "arrived" in joined, notes
    s, ov = t.json("GET", "/api/me/overview")
    assert ov["trip"] is None
    ok("Trip from the web: tracking start → stop alert → arrival, trip auto-completed")

    # ── timer & settings ──
    s, body = t.json("POST", "/api/me/timer", {"minutes": 60})
    assert s == 200 and body["due_ts"]
    s, body = t.json("POST", "/api/me/timer", {"minutes": 2})
    assert s == 400
    s, _ = t.json("POST", "/api/me/settings", {"monitoring": False, "community": False})
    s, ov = t.json("GET", "/api/me/overview")
    assert ov["timer_due_ts"] and not ov["monitoring"] and not ov["community"]
    ok("Safety timer and settings saved (invalid timer rejected)")

    # ── guardians from the web ──
    s, body = t.json("POST", "/api/me/guardians", {"name": "Mum"})
    assert body["link"].startswith("https://t.me/TravelBuddyTestBot?start=g_")
    ok("Guardian invite link created from the web")

    # ── officer: command center & actions ──
    o = Client(base)
    me = sign_in(o, "9001", ADMIN)
    assert me["is_admin"]
    s, _, raw = o.req("GET", "/command")
    assert s == 200 and b"Command Center" in raw
    s, body = t.json("POST", "/api/me/report", {"type": "scam", "description": "Fake guide demanded money", "use_location": True})
    scam_id = body["incident"]["id"]
    anon = Client(base)
    s, body = anon.json("POST", f"/api/incident/{scam_id}/resolve", headers={"X-TB-Token": "desk-token-xyz"})
    assert s == 200 and body["incident"]["status"] == "resolved"
    s, _ = anon.json("POST", f"/api/incident/{scam_id}/ack", headers={"X-TB-Token": "wrong"})
    assert s == 401
    ok("Officer signs in to the command center; desk token resolves incidents; bad token refused")

    # ── Assistant: copilot, safety briefing, phrasebook, travel guide ──
    s, body = t.json("POST", "/api/me/ask", {"question": "is it safe to walk to the fort now?"})
    assert s == 200 and not body["distress"] and "offline mode" in body["html"]
    s, body = t.json("POST", "/api/me/ask", {"question": "help me please someone is following me"})
    assert body["distress"] and "SOS" in body["html"]
    from travel_buddy.ai import copilot as _cop

    async def evil_answer(q, ctx, channel="web"):
        return "<script>alert(1)</script><b>bold</b> <a href='javascript:x'>link</a>"
    real_answer, _cop.answer_copilot = _cop.answer_copilot, evil_answer
    s, body = t.json("POST", "/api/me/ask", {"question": "hello"})
    _cop.answer_copilot = real_answer
    assert "<script>" not in body["html"] and "&lt;script&gt;" in body["html"] and "<b>bold</b>" in body["html"] \
        and "<a " not in body["html"]
    ok("Copilot chat answers, flags distress, and AI output is sanitised (no script/link injection)")

    from travel_buddy.ai import triage as _tri

    async def fake_brief(place, extra=""):
        return f"<b>🛡️ Overall</b> {place} is generally safe."
    _tri.generate_safety_briefing = fake_brief
    s, body = t.json("POST", "/api/me/safetytips", {"place": ""})
    assert s == 200 and body["place"] and "<b>" in body["html"]
    s, body = t.json("GET", "/api/me/phrases")
    assert body["auto"] == "hindi" and "मदद करो" in body["phrases"]["hindi"][0] and len(body["keys"]) == 5
    s, body = t.json("POST", "/api/me/phrases/translate", {"language": "Assamese", "text": "Help"})
    assert s == 200 and "GROQ_API_KEY" in body["result"]
    ok("Safety briefing (defaults to your area), phrasebook auto-language (Hindi in India), AI translate fallback")

    s, body = t.json("GET", "/api/me/preferences")
    assert body["preferences"] is None
    s, body = t.json("POST", "/api/me/explore", {"query": "street food"})
    assert s == 409
    s, body = t.json("POST", "/api/me/preferences", {"interests": "food, history", "budget_level": "low", "pace": "relaxed"})
    assert s == 200 and body["preferences"]["budget_level"] == "low"
    s, body = t.json("POST", "/api/me/preferences", {"interests": "x", "budget_level": "cheap", "pace": "relaxed"})
    assert s == 400
    s, body = t.json("POST", "/api/me/explore", {"query": "street food"})
    assert s == 200 and "Groq API Key Not Configured" in body["html"]
    ok("Travel guide needs preferences first; preferences validated and saved; guide answers")

    # ── photo evidence upload ──
    import base64
    from travel_buddy.bot.intel_handlers import make_qr_png
    png = make_qr_png("evidence-test")
    s, body = t.json("POST", "/api/me/evidence", {"image_b64": "data:image/png;base64," + base64.b64encode(png).decode(),
                                                  "caption": "Taxi that took my bag"})
    assert s == 200 and body["sha256"] and body["incident"]["id"], body
    ev_rows = runtime.run(store.get_evidence(body["incident"]["id"]))
    assert ev_rows and ev_rows[-1]["file_id"].startswith("tg-photo-"), "web photo should be stored via Telegram"
    assert any(c == TOURIST and txt.startswith("[photo] 📸 Evidence saved") for c, txt in BOT.sent)
    s, body = t.json("POST", "/api/me/evidence", {"image_b64": base64.b64encode(b"not an image").decode()})
    assert s == 400
    s, body = x.json("POST", "/api/me/evidence", {"image_b64": base64.b64encode(png).decode(), "incident_id": sos_id})
    assert s == 404, "can't attach evidence to someone else's incident"
    ok("Photo evidence upload sealed on the ledger; non-images and other people's incidents refused")

    # ── safety desk: zones & usage ──
    s, body = o.json("GET", "/api/desk/usage")
    assert s == 200 and {u["service"] for u in body["usage"]} == {"ai", "weather", "routing", "places"}
    s, body = o.json("POST", "/api/desk/zones", {"name": "Test Cantonment", "risk": "restricted", "radius_m": 1500,
                                                 "lat": 26.95, "lng": 75.80, "advisory": "No entry"})
    assert s == 200 and body["zone"]["risk"] == "restricted"
    zid = body["zone"]["id"]
    s, body = anon.json("GET", "/api/desk/zones", headers={"X-TB-Token": "desk-token-xyz"})
    assert any(z["id"] == zid for z in body["zones"])
    s, body = o.json("POST", "/api/desk/zones", {"name": "Bad", "risk": "restricted", "radius_m": 10, "lat": 0, "lng": 0})
    assert s == 400
    s, _ = x.json("GET", "/api/desk/zones")
    assert s == 403, "non-officers can't manage zones"
    s, body = t.json("POST", "/api/me/location", {"lat": 26.95, "lng": 75.80})
    assert any("Test Cantonment" in n for n in body["notes"]), body
    s, _ = o.json("POST", f"/api/desk/zones/{zid}/remove")
    s, body = o.json("GET", "/api/desk/zones")
    assert not any(z["id"] == zid for z in body["zones"])
    ok("Officer adds a zone (tourists alerted on entry), lists usage, removes it; strangers refused")

    # ── logout ──
    s, _ = t.json("POST", "/api/auth/logout")
    s, _ = t.json("GET", "/api/me")
    assert s == 401
    ok("Logout ends the session")

    chain = runtime.run(ledger.verify_chain())
    assert chain["valid"]
    ok(f"Ledger intact after all web actions ({chain['length']} blocks)")
    print("=" * 68)
    print("ALL WEB PORTAL TESTS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    main()
