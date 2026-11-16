"""
test_intel.py — Tests for the next-gen intelligence layer (offline, temporary database).

Covers: self-learning hotspot clustering, weather hazard assessment, safe-route scoring,
multilingual distress detection, community alerts, weather watch in the monitor, evidence
sealing, QR generation and the web portal (public pages + token-protected command center).

Run:  .\\venv\\Scripts\\python test_intel.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ["SEED_STARTER_ZONES"] = "1"  # tests use the starter zones
os.environ["ADMIN_IDS"] = "9001"
os.environ["GROQ_API_KEY"] = ""
os.environ["DASHBOARD_TOKEN"] = "test-token-123"

from travel_buddy.safety import ledger as blockchain_id
from travel_buddy.core import database
from travel_buddy.safety import store as safety_db
TMP_DB = os.path.join(tempfile.mkdtemp(), "intel_test.db")
for mod in (database, safety_db, blockchain_id):
    mod.DB_PATH = TMP_DB

from travel_buddy.safety import incidents as incident_service
from travel_buddy.web import dashboard as web_dashboard
from travel_buddy.ai.copilot import detect_distress, phrasebook_text
from travel_buddy.bot.intel_handlers import make_qr_png, _seal_evidence
from travel_buddy.safety.risk_intel import assess_route, assess_weather, cluster_hotspots, hotspots_near

TOURIST, NEIGHBOUR, FAR_USER, ADMIN = 1001, 1002, 1003, 9001


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_location(self, *a, **kw):
        pass


def ok(msg):
    print(f"[OK] {msg}")


def iso_days_ago(d):
    return (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()


def test_hotspots():
    base = dict(incident_type="theft", severity="medium")
    incidents = [
        {**base, "lat": 26.9239, "lng": 75.8267, "created_at": iso_days_ago(1)},
        {**base, "lat": 26.9250, "lng": 75.8270, "created_at": iso_days_ago(2)},
        {**base, "lat": 26.9230, "lng": 75.8280, "created_at": iso_days_ago(0)},
        {**base, "lat": 26.9245, "lng": 75.8260, "severity": "high", "created_at": iso_days_ago(3)},
        {**base, "lat": 27.5000, "lng": 76.5000, "created_at": iso_days_ago(1)},  # isolated → noise
    ]
    hs = cluster_hotspots(incidents)
    assert len(hs) == 1 and hs[0]["count"] == 4 and hs[0]["dominant_type"] == "theft"
    assert hs[0]["risk_level"] == "high", hs[0]
    old = [{**i, "created_at": iso_days_ago(29)} for i in incidents[:2]]
    assert cluster_hotspots(old)[0]["score"] < hs[0]["score"], "older incidents must weigh less"
    assert hotspots_near(26.9240, 75.8268, hs, within_m=300)
    assert not hotspots_near(26.99, 75.90, hs, within_m=300)
    ok(f"Self-learning hotspots: 4 clustered thefts → {hs[0]['risk_level']} (score {hs[0]['score']}), noise ignored")
    return hs


def test_weather():
    calm = assess_weather({"current": {"temperature_2m": 24, "apparent_temperature": 24, "weather_code": 1,
                                       "precipitation": 0, "wind_gusts_10m": 12},
                           "hourly": {"weather_code": [1, 2], "precipitation": [0, 0],
                                      "wind_gusts_10m": [10, 15], "temperature_2m": [24, 23]}})
    assert calm["worst"] == "none" and not calm["hazards"]
    storm = assess_weather({"current": {"temperature_2m": 29, "weather_code": 3, "precipitation": 0,
                                        "wind_gusts_10m": 30},
                            "hourly": {"weather_code": [63, 95], "precipitation": [2, 12],
                                       "wind_gusts_10m": [40, 85], "temperature_2m": [29, 27]}})
    keys = {h["key"] for h in storm["hazards"]}
    assert storm["worst"] == "high" and {"thunderstorm", "heavy_rain", "wind"} <= keys, keys
    ok(f"Weather hazards: calm → none, storm → {sorted(keys)}")
    return storm


def test_route(hotspots, storm):
    zone = {"zone_id": 1, "name": "Restricted Park", "center_lat": 26.50, "center_lng": 93.00,
            "radius_m": 3000, "risk_level": "restricted", "advisory": ""}
    safe = assess_route([(26.0, 92.0), (26.05, 92.05)], [zone], hotspots, local_hour=14)
    assert safe["score"] == 100 and safe["verdict"].startswith("✅")
    through = assess_route([(26.40, 93.00), (26.60, 93.00)], [zone], hotspots, local_hour=23, weather=storm)
    assert through["verdict"].startswith("⛔") and through["score"] <= 20, through
    via_hot = assess_route([(26.915, 75.82), (26.935, 75.835)], [], hotspots, local_hour=12)
    assert via_hot["hotspots"] and via_hot["score"] == 80
    ok(f"Safe-route scoring: clear=100, via hotspot={via_hot['score']}, restricted+night+storm={through['score']} ⛔")


def test_language():
    for s in ["help me please", "Bachao! koi hai", "मुझे मदद चाहिए", "au secours", "助けて", "someone call the police"]:
        assert detect_distress(s), s
    for s in ["where can I get good coffee?", "is the fort open today", "helpful tips for Jaipur"]:
        assert not detect_distress(s), s
    assert "मदद करो" in phrasebook_text("hindi") and phrasebook_text("klingon") is None
    ok("Multilingual distress detection (EN/HI/FR/JA) + offline phrasebook")


async def test_community_and_weather_watch():
    for uid in (TOURIST, NEIGHBOUR, FAR_USER):
        await database.upsert_user(uid, f"user{uid}")
    now = time.time()
    await safety_db.update_safety_state(NEIGHBOUR, last_lat=26.9250, last_lng=75.8275, last_seen_ts=now)
    await safety_db.update_safety_state(FAR_USER, last_lat=28.6, last_lng=77.2, last_seen_ts=now)
    bot = FakeBot()
    inc = await incident_service.raise_incident(bot, TOURIST, "theft", "medium", "bag snatched",
                                                26.9239, 75.8267, source="report")
    community = [c for c, t in bot.messages if "Community safety alert" in t]
    assert community == [NEIGHBOUR] and inc["community_alerted"] == 1, bot.messages
    assert "user1001" not in " ".join(t for _, t in bot.messages if "Community" in t)
    ok("Community alert reached the nearby traveller only, anonymously")

    await safety_db.update_safety_state(NEIGHBOUR, community_alerts=0)
    bot = FakeBot()
    await incident_service.raise_incident(bot, TOURIST, "scam", "medium", "fake guide", 26.9239, 75.8267, source="report")
    assert not [1 for _, t in bot.messages if "Community" in t]
    ok("/community off is respected")

    calls = []

    async def fake_weather(lat, lng):
        calls.append((lat, lng))
        return {"worst": "high", "summary": "thunderstorm, 27°C",
                "hazards": [{"key": "thunderstorm", "severity": "high", "text": "⛈️ Thunderstorm expected"}]}

    incident_service.weather_provider = fake_weather
    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot, now=time.time())
    alerted = [c for c, t in bot.messages if "Severe weather" in t]
    assert NEIGHBOUR in alerted and FAR_USER in alerted, bot.messages
    bot = FakeBot()
    await incident_service.run_monitor_cycle(bot, now=time.time() + 31 * 60)
    assert not [1 for _, t in bot.messages if "Severe weather" in t], "same hazard must not repeat"
    ok(f"Severe-weather watch alerts recently-active users once ({len(calls)} lookups)")


async def test_evidence_qr_and_web():
    rec = await blockchain_id.issue_tourist_id(TOURIST, "Asha Verma", "Indian", "Passport", "Z1234567",
                                              "Ravi", "+91 98765 43210", 7)
    digest, block = await _seal_evidence(TOURIST, 1, "photo", "file-abc", b"\x89PNG fake image bytes")
    ev = await safety_db.get_evidence(1)
    assert ev and ev[0]["sha256"] == digest and block["block_type"] == "EVIDENCE"
    assert (await blockchain_id.verify_chain())["valid"]
    ok("Photo evidence fingerprint sealed on ledger and linked to incident")

    png = make_qr_png(f"http://localhost/verify/{rec['tourist_id']}")
    assert png and png[:4] == b"\x89PNG"
    ok("QR code generated for Digital ID verification URL")

    server = web_dashboard.start_web_server("127.0.0.1", 0)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()

    try:
        s, body = await asyncio.to_thread(get, "/")
        assert s == 200 and "Tourist Safety Portal" in body
        s, body = await asyncio.to_thread(get, f"/verify/{rec['tourist_id']}")
        assert "VALID" in body and "Asha Verma" in body and "Z1234567" not in body
        s, body = await asyncio.to_thread(get, "/verify/TID-NOPE")
        assert "Not found" in body
        s, body = await asyncio.to_thread(get, "/ledger")
        assert "INTACT" in body and "EVIDENCE" in body
        try:
            await asyncio.to_thread(get, "/api/overview?token=wrong")
            raise AssertionError("command center must reject a bad token")
        except HTTPError as e:
            assert e.code == 403
        s, body = await asyncio.to_thread(get, "/api/overview?token=test-token-123")
        data = json.loads(body)
        assert data["stats"]["open_incidents"] >= 2 and data["ledger"]["valid"]
        assert any(t["id"] == NEIGHBOUR for t in data["tourists"])
        s, body = await asyncio.to_thread(get, "/command?token=test-token-123")
        assert "leaflet" in body and "Command Center" in body
        ok(f"Web portal: landing, public ID verify, ledger explorer, token-protected command center (port {port})")
    finally:
        server.shutdown()


async def test_wiring():
    import importlib
    importlib.import_module("main")
    importlib.import_module("travel_buddy.bot.intel_handlers")
    ok("Bot modules import cleanly")


async def run():
    print("=" * 68)
    print(">>> NEXT-GEN INTELLIGENCE TESTS")
    print("=" * 68)
    await database.init_db()
    await safety_db.init_safety_db()
    await blockchain_id.ensure_genesis()
    hs = test_hotspots()
    storm = test_weather()
    test_route(hs, storm)
    test_language()
    await test_community_and_weather_watch()
    await test_evidence_qr_and_web()
    await test_wiring()
    print("=" * 68)
    print("ALL NEXT-GEN TESTS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(run())
