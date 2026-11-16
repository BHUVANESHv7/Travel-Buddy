"""
test_safety.py — Tests for the Smart Tourist Safety layer (runs on a temporary database).

Covers: blockchain ledger & digital ID (incl. tamper detection), geo-fencing events,
AI anomaly detection, safety score, incident triage, responder alerts, and the
background check-in / escalation monitor.

Run:  .\\venv\\Scripts\\python test_safety.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import os
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["SEED_STARTER_ZONES"] = "1"  # tests use the starter zones
os.environ["ADMIN_IDS"] = "9001"
os.environ["GROQ_API_KEY"] = ""  # force offline rule-based triage

import aiosqlite

from travel_buddy.safety import ledger as blockchain_id
from travel_buddy.core import database
from travel_buddy.safety import store as safety_db
TMP_DB = os.path.join(tempfile.mkdtemp(), "safety_test.db")
for mod in (database, safety_db, blockchain_id):
    mod.DB_PATH = TMP_DB

from travel_buddy.safety import incidents as incident_service
from travel_buddy.ai.triage import keyword_triage
from travel_buddy.safety.geofence import evaluate_geofence
from travel_buddy.safety.monitor import analyze_motion, compute_safety_score, reset_motion

TOURIST, GUARDIAN, ADMIN = 1001, 2002, 9001


class FakeBot:
    def __init__(self):
        self.messages, self.locations = [], []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    async def send_location(self, chat_id, latitude, longitude, **kwargs):
        self.locations.append((chat_id, latitude, longitude))


def ok(msg):
    print(f"[OK] {msg}")


async def test_ledger_and_id():
    await blockchain_id.ensure_genesis()
    rec = await blockchain_id.issue_tourist_id(
        TOURIST, "Asha Verma", "Indian", "Passport", "Z1234567", "Ravi Verma", "+91 98765 43210", 7)
    assert rec["tourist_id"].startswith("TID-")
    assert rec["block_hash"].startswith("0" * blockchain_id.DIFFICULTY)
    stored = await blockchain_id.get_tourist_id(rec["tourist_id"])
    assert "Z1234567" not in str(stored), "raw document number must never be stored"
    assert stored["doc_last4"] == "4567"
    ok(f"Digital ID issued {rec['tourist_id']} (PoW block, doc number only hashed)")

    v = await blockchain_id.verify_tourist_id(rec["tourist_id"])
    assert v["valid"], v["reasons"]
    ok("Digital ID verifies against ledger")

    async with aiosqlite.connect(TMP_DB) as db:
        await db.execute("UPDATE tourist_ids SET full_name = 'Mallory' WHERE tourist_id = ?", (rec["tourist_id"],))
        await db.commit()
    v = await blockchain_id.verify_tourist_id(rec["tourist_id"])
    assert not v["valid"] and any("altered" in r for r in v["reasons"])
    async with aiosqlite.connect(TMP_DB) as db:
        await db.execute("UPDATE tourist_ids SET full_name = 'Asha Verma' WHERE tourist_id = ?", (rec["tourist_id"],))
        await db.commit()
    ok("Tampered ID record detected (identity hash mismatch)")

    async with aiosqlite.connect(TMP_DB) as db:
        await db.execute("UPDATE ledger SET payload = '{\"hacked\":1}' WHERE block_index = 1")
        await db.commit()
    chain = await blockchain_id.verify_chain()
    assert not chain["valid"] and chain["broken_at"] == 1
    ok("Tampered ledger block detected (chain broken at #1)")
    # Restore a clean ledger + ID for the remaining tests.
    async with aiosqlite.connect(TMP_DB) as db:
        await db.execute("DELETE FROM ledger")
        await db.execute("DELETE FROM tourist_ids")
        await db.commit()
    await blockchain_id.issue_tourist_id(
        TOURIST, "Asha Verma", "Indian", "Passport", "Z1234567", "Ravi Verma", "+91 98765 43210", 7)
    assert (await blockchain_id.verify_chain())["valid"]


def test_geofence():
    zone = {"zone_id": 1, "name": "Test restricted", "center_lat": 26.0, "center_lng": 93.0,
            "radius_m": 2000, "risk_level": "restricted", "advisory": ""}
    state = {"inside": [], "near": []}
    ev, state = evaluate_geofence(26.03, 93.0, [zone], state)       # ~3.3 km from centre → 1.3 km out
    assert ev == []
    ev, state = evaluate_geofence(26.026, 93.0, [zone], state)      # ~0.9 km from edge
    assert [e.kind for e in ev] == ["APPROACH"]
    ev, state = evaluate_geofence(26.005, 93.0, [zone], state)      # inside
    assert [e.kind for e in ev] == ["ENTER"]
    ev, state = evaluate_geofence(26.005, 93.0, [zone], state)      # still inside → no repeat
    assert ev == []
    ev, state = evaluate_geofence(26.05, 93.0, [zone], state)       # left
    assert [e.kind for e in ev] == ["EXIT"]
    ok("Geo-fence APPROACH → ENTER → (no duplicate) → EXIT events")


def test_anomalies():
    trip = {"trip_id": 1, "status": "active", "destination_lat": 27.0, "destination_lng": 75.0}
    uid, t = 5, 1_000_000.0
    reset_motion(uid)
    # Moving north at ~72 km/h (0.01° lat ≈ 1.11 km per 55 s).
    lat = 26.0
    for i in range(4):
        assert analyze_motion(uid, lat, 75.0, trip, ts=t) == []
        lat += 0.01
        t += 55
    # Sudden halt, stays put for 4 minutes.
    found = []
    for _ in range(5):
        found += analyze_motion(uid, lat, 75.0, trip, ts=t)
        t += 60
    assert [a.kind for a in found] == ["ABRUPT_HALT"], found
    ok("Abrupt halt detected after fast travel")

    for _ in range(20):
        found += analyze_motion(uid, lat, 75.0, trip, ts=t)
        t += 60
    assert [a.kind for a in found] == ["ABRUPT_HALT"], "prolonged stop should not double-alert same stop"

    reset_motion(uid)
    t = 2_000_000.0
    for i in range(30):  # slow bus, stationary 25 min, no abrupt halt
        found2 = analyze_motion(uid, 26.5, 75.0, trip, ts=t)
        t += 60
        if found2:
            break
    assert found2 and found2[0].kind == "PROLONGED_STOP"
    ok("Prolonged mid-journey stop detected")

    reset_motion(uid)
    t, lat, kinds = 3_000_000.0, 26.5, []
    for i in range(6):  # moving south, away from destination at 27.0
        kinds += [a.kind for a in analyze_motion(uid, lat, 75.0, trip, ts=t)]
        lat -= 0.006
        t += 60
    assert "ROUTE_DEVIATION" in kinds, kinds
    ok("Route deviation detected")


def test_score_and_triage():
    s = compute_safety_score()
    assert s["score"] == 100 and s["label"] == "Safe"
    s = compute_safety_score(zone_risk="restricted", local_hour=23, checkin_pending=True)
    assert s["score"] == 30 and s["label"] == "Elevated risk"
    ok(f"Safety score: baseline 100, restricted+night+pending check-in = {s['score']}")

    assert keyword_triage("Someone snatched my wallet near the market")["category"] == "theft"
    assert keyword_triage("my friend is unconscious and not breathing")["severity"] == "critical"
    assert keyword_triage("a man is following me with a knife", "harassment")["severity"] == "critical"
    assert keyword_triage("taxi driver overcharged", "scam")["severity"] == "low"
    ok("Rule-based AI triage fallback classifies incidents")


async def test_incidents_and_monitor():
    await database.upsert_user(TOURIST, "asha")
    await database.upsert_user(GUARDIAN, "ravi")
    await safety_db.create_guardian_invite(TOURIST, "Ravi", "code123")
    assert await safety_db.link_guardian("code123", GUARDIAN)
    assert await safety_db.link_guardian("code123", 3003) is None, "invite must be single-use"
    ok("Guardian invite link works once")

    bot = FakeBot()
    inc = await incident_service.raise_incident(
        bot, TOURIST, "sos", "critical", "Panic button pressed.", 26.9, 75.8, source="sos")
    recipients = {chat for chat, _ in bot.messages}
    assert recipients == {GUARDIAN, ADMIN}, recipients
    assert len(bot.locations) == 2 and inc["block_hash"]
    assert "TID-" in bot.messages[0][1] and "+91" in bot.messages[0][1]
    ok("SOS sealed on ledger and sent to guardian + safety desk with location")

    bot = FakeBot()
    await incident_service.raise_incident(bot, TOURIST, "scam", "low", "overcharged", None, None, source="report")
    assert bot.messages == [], "low severity should not page responders"
    ok("Low-severity reports are logged without paging responders")

    # ── Background monitor: inactivity → check-in → escalation ──
    trip_id = await database.create_trip(TOURIST, "Jaipur", 26.9, 75.8, "train")
    await database.activate_trip(trip_id, TOURIST)
    now = time.time()
    await safety_db.update_safety_state(TOURIST, last_lat=26.5, last_lng=75.5,
                                        last_seen_ts=now - incident_service.INACTIVITY_S - 5)
    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot, now=now)
    assert bot.messages and bot.messages[0][0] == TOURIST and "check-in" in bot.messages[0][1].lower()
    ok("Lost-signal check-in sent during active trip")

    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot, now=now + 60)
    assert bot.messages == [], "no duplicate prompt / premature escalation"

    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot, now=time.time() + incident_service.ESCALATE_S + 5)
    recipients = [c for c, _ in bot.messages]
    assert GUARDIAN in recipients and ADMIN in recipients and TOURIST in recipients
    open_types = [i["incident_type"] for i in await safety_db.get_open_user_incidents(TOURIST)]
    assert "no_response" in open_types
    ok("Unanswered check-in escalated to guardian + safety desk as an incident")

    # ── Safety timer ──
    await safety_db.update_safety_state(TOURIST, timer_due_ts=time.time() - 1, checkin_pending_ts=None)
    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot)
    assert bot.messages and "safety timer" in bot.messages[0][1].lower()
    ok("Expired safety timer triggers a check-in")

    chain = await blockchain_id.verify_chain()
    assert chain["valid"]
    ok(f"Ledger intact after all events ({chain['length']} blocks)")


async def test_wiring():
    __import__("main")
    __import__("travel_buddy.bot.safety_handlers")
    ok("Bot modules import cleanly")


async def run():
    print("=" * 68)
    print(">>> SMART TOURIST SAFETY TESTS")
    print("=" * 68)
    await database.init_db()
    await safety_db.init_safety_db()
    await test_ledger_and_id()
    test_geofence()
    test_anomalies()
    test_score_and_triage()
    await test_incidents_and_monitor()
    await test_wiring()
    print("=" * 68)
    print("ALL SAFETY TESTS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(run())
