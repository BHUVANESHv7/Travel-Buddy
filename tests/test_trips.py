import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import os
from travel_buddy.core import database

# Isolated database so the test is repeatable and never touches real data
import tempfile as _tempfile
database.DB_PATH = os.path.join(_tempfile.mkdtemp(), "test.db")
from travel_buddy.core import geocoding
async def test_all():
    # Uses the temporary database configured above — never delete the real travel_bot.db.
    await database.init_db()

    # 1. User creation
    await database.upsert_user(1001, "tester")
    user = await database.get_user(1001)
    assert user["current_trip_status"] == "no_active_trip"
    print("[OK] User creation and default status check passed")

    # 2. No ongoing trip
    ongoing = await database.get_active_or_planning_trip(1001)
    assert ongoing is None
    print("[OK] Initial ongoing trip check passed")

    # 3. Create trip with status 'planning'
    trip_id = await database.create_trip(1001, "Gare du Nord", 48.8809, 2.3553, "train")
    assert trip_id == 1
    ongoing = await database.get_active_or_planning_trip(1001)
    assert ongoing is not None
    assert ongoing["status"] == "planning"
    assert ongoing["destination_name"] == "Gare du Nord"
    assert ongoing["transport_mode"] == "train"

    # Verify users.current_trip_status was synced
    user = await database.get_user(1001)
    assert user["current_trip_status"] == "planning"
    print("[OK] Create trip and status synchronization check passed")

    # 4. Complete trip (/endtrip)
    completed = await database.complete_trip(1001)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["completed_at"] is not None

    user = await database.get_user(1001)
    assert user["current_trip_status"] == "no_active_trip"
    assert await database.get_active_or_planning_trip(1001) is None
    print("[OK] Complete trip (/endtrip) and reset to no_active_trip passed")

    # 5. Cancel trip (/canceltrip)
    trip_id2 = await database.create_trip(1001, "Victoria Station", 51.4952, -0.1439, "bus")
    user = await database.get_user(1001)
    assert user["current_trip_status"] == "planning"

    cancelled = await database.cancel_trip(1001)
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["completed_at"] is not None

    user = await database.get_user(1001)
    assert user["current_trip_status"] == "no_active_trip"
    assert await database.get_active_or_planning_trip(1001) is None
    print("[OK] Cancel trip (/canceltrip) and reset to no_active_trip passed")

    # 6. Geocoding integration test
    res = await geocoding.geocode_location("Zurich HB")
    assert res is not None
    lat, lng, name = res
    print(f"[OK] Geocoding test passed: Zurich HB -> ({lat:.4f}, {lng:.4f})")

    print("\nALL DATABASE, LIFECYCLE & STATE TRANSITION TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(test_all())
