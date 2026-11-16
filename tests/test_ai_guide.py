"""
test_ai_guide.py — Test suite for the AI travel guide: User Preferences, DuckDuckGo Search, and AI Travel Guide.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from travel_buddy.core import database

# Isolated database so the test is repeatable and never touches real data
database.DB_PATH = os.path.join(tempfile.mkdtemp(), "ai_guide_test.db")
from travel_buddy.ai.travel_guide import (
    search_travel_web,
    format_search_snippets,
    TRAVEL_GUIDE_SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    generate_travel_guide,
)


async def run_tests():
    print("=" * 68)
    print(">>> RUNNING PHASE 4: AI TRAVEL GUIDE & PREFERENCES TESTS")
    print("=" * 68)

    # 1. Database tests for user_preferences
    await database.init_db()
    test_user_id = 7777
    await database.upsert_user(test_user_id, "travel_enthusiast")

    # Verify initial preferences is None
    init_pref = await database.get_user_preferences(test_user_id)
    assert init_pref is None, "Expected initial preferences to be None"
    print("[OK] Initial preferences check passed: None")

    # Save preferences
    await database.save_user_preferences(
        telegram_id=test_user_id,
        interests="food, street photography, history",
        budget_level="medium",
        pace="relaxed",
    )

    saved_pref = await database.get_user_preferences(test_user_id)
    assert saved_pref is not None
    assert saved_pref["interests"] == "food, street photography, history"
    assert saved_pref["budget_level"] == "medium"
    assert saved_pref["pace"] == "relaxed"
    print("[OK] Preferences insertion check passed")

    # Update preferences
    await database.save_user_preferences(
        telegram_id=test_user_id,
        interests="nightlife, architecture",
        budget_level="high",
        pace="packed",
    )
    updated_pref = await database.get_user_preferences(test_user_id)
    assert updated_pref["interests"] == "nightlife, architecture"
    assert updated_pref["budget_level"] == "high"
    assert updated_pref["pace"] == "packed"
    print("[OK] Preferences update (ON CONFLICT) check passed")

    # 2. DuckDuckGo Search integration test
    print("\nTesting DuckDuckGo search integration for 'Jaipur street food'...")
    search_results = []
    for attempt in range(3):  # DuckDuckGo rate-limits automated searches now and then
        search_results = await search_travel_web("Jaipur top street food", max_results=3)
        if search_results:
            break
        await asyncio.sleep(3 * (attempt + 1))
    if search_results:
        print(f"[OK] DuckDuckGo search passed: retrieved {len(search_results)} fresh results")
        print(f"     First result title: {search_results[0]['title']}")
    else:
        # External service unavailable: verify the guide degrades gracefully instead of failing.
        fallback = format_search_snippets([])
        assert "No real-time web results" in fallback
        print("[OK] DuckDuckGo unavailable (rate-limited) — offline fallback text verified")
        search_results = [{"title": "Sample", "snippet": "Sample snippet for prompt formatting.", "url": ""}]

    # 3. Prompt construction formatting test
    snippets = format_search_snippets(search_results)
    assert len(snippets) > 20
    assert "[1]" in snippets

    sys_prompt = TRAVEL_GUIDE_SYSTEM_PROMPT.format(
        interests=updated_pref["interests"],
        budget=updated_pref["budget_level"].capitalize(),
        pace=updated_pref["pace"].capitalize(),
    )
    assert "nightlife, architecture" in sys_prompt
    assert "Packed" in sys_prompt

    user_prompt = USER_PROMPT_TEMPLATE.format(
        destination_context="Jaipur",
        interests=updated_pref["interests"],
        budget=updated_pref["budget_level"].capitalize(),
        pace=updated_pref["pace"].capitalize(),
        user_query="Where should I eat tonight?",
        search_snippets=snippets,
    )
    assert "Jaipur" in user_prompt
    assert "Where should I eat tonight?" in user_prompt
    print("[OK] Prompt template formatting verified")

    # 4. Graceful handling when GROQ_API_KEY is not set or placeholder
    test_reply = await generate_travel_guide(
        user_query="Best cafes",
        destination_context="Jaipur",
        preferences=updated_pref,
        api_key=None,
    )
    assert "Groq API Key Not Configured" in test_reply or "GROQ_API_KEY" in test_reply
    print("[OK] Missing/Placeholder API key safety check passed")

    print("\n" + "=" * 68)
    print("ALL PHASE 4 TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(run_tests())
