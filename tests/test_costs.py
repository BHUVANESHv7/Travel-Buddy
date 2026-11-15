"""
test_costs.py — Proves the zero-cost guards work (offline, temporary database).

  - Daily free-tier caps are enforced and persist across restarts
  - When the AI cap is reached, features fall back offline and Groq is NOT called
  - OpenStreetMap Nominatim calls are spaced ≥ 1 s apart (usage policy)
  - TTL caches avoid repeat network calls

Run:  python tests/test_costs.py
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

os.environ["GROQ_API_KEY"] = "gsk_fake_key_for_tests_only"  # looks real; must never be used past the cap
os.environ["AI_DAILY_LIMIT"] = "2"

from travel_buddy.core import database

database.DB_PATH = os.path.join(tempfile.mkdtemp(), "costs_test.db")

from travel_buddy.core import usage
from travel_buddy.ai import triage


def ok(msg):
    print(f"[OK] {msg}")


def test_caps():
    usage.DAILY_LIMITS["weather"] = 3
    assert [usage.try_consume("weather") for _ in range(5)] == [True, True, True, False, False]
    assert usage.used_today("weather") >= 3
    ok("Daily cap enforced (3 allowed, then blocked)")

    # A "restart" (fresh module state) still sees today's count, because it's stored in SQLite.
    import importlib
    importlib.reload(usage)
    usage.DAILY_LIMITS["weather"] = 3
    assert usage.try_consume("weather") is False
    ok("Cap survives a restart (persisted in the database)")


async def test_ai_cap_fallback():
    calls = []

    class Boom:
        def __init__(self, *a, **k):
            calls.append("groq")
            raise AssertionError("Groq must not be called after the cap")

    triage.AsyncGroq = Boom  # any real AI call past the cap would be detected
    usage.DAILY_LIMITS["ai"] = 2
    # Use up the cap (these two would be real calls → our Boom records + falls back via except)
    for _ in range(2):
        r = await triage.classify_incident("my wallet was stolen", "theft")
        assert r["engine"] == "rules"
    calls.clear()
    r = await triage.classify_incident("someone is following me with a knife", "harassment")
    assert r["engine"] == "rules" and r["severity"] == "critical"
    assert calls == [], "Groq was contacted after the free cap"
    ok("AI cap reached → offline rule-based triage, no Groq call (still flags critical danger)")

    from travel_buddy.ai.copilot import answer_copilot
    txt = await answer_copilot("is it safe here?", {"place": "Jaipur", "safety_score": "80/100"})
    assert "offline mode" in txt and calls == []
    ok("Copilot answers offline after the cap")

    from travel_buddy.ai.travel_guide import generate_travel_guide
    txt = await generate_travel_guide("food", "Jaipur", {})
    assert "limit" in txt.lower() and calls == []
    ok("AI travel guide pauses politely after the cap")


async def test_nominatim_spacing():
    usage._last_nominatim = 0.0
    t0 = time.monotonic()
    await asyncio.gather(*(usage.nominatim_slot() for _ in range(3)))
    elapsed = time.monotonic() - t0
    assert elapsed >= 2 * usage.NOMINATIM_INTERVAL_S - 0.05, elapsed
    ok(f"Nominatim requests spaced ≥1 s apart (3 calls took {elapsed:.1f}s)")


def test_cache():
    c = usage.TTLCache(ttl_s=0.3, max_items=4)
    c.set("a", 1)
    assert c.get("a") == 1
    time.sleep(0.35)
    assert c.get("a") is None
    for i in range(10):
        c.set(i, i)
    assert len(c._d) <= 4
    ok("TTL cache expires entries and stays bounded")


async def run():
    print("=" * 68)
    print(">>> ZERO-COST GUARD TESTS")
    print("=" * 68)
    test_caps()
    await test_ai_cap_fallback()
    await test_nominatim_spacing()
    test_cache()
    print("=" * 68)
    print("ALL COST TESTS PASSED")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(run())
