"""
simulate_live_trip.py — Live Location Simulation and Verification Engine.

This script simulates a complete train journey approaching a destination stop,
testing and demonstrating:
  1. Transition from 'planning' to 'active' on first live location ping
  2. Ground speed calculation across sequential GPS pings
  3. Dynamic ETA calculation based on real movement
  4. Proximity Alert trigger (fires when ETA <= 4 min or distance <= 1 km)
  5. Duplicate alert prevention (verifies alert fires ONLY ONCE)
  6. Automatic arrival completion (distance <= 150m)
  7. Overshoot detection (passing the station without disembarking)

Run this script directly to verify all Phase 3 tracking and alert mechanics:
    python simulate_live_trip.py
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import asyncio
import os
os.environ["SEED_STARTER_ZONES"] = "1"  # tests use the starter zones
import sys
import time

# Ensure unicode/emoji print safely on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from travel_buddy.core.tracker import (
    process_location_ping,
    remove_tracker,




)
from travel_buddy.core import database

# Isolated database so the test is repeatable and never touches real data
import tempfile as _tempfile
database.DB_PATH = os.path.join(_tempfile.mkdtemp(), "test.db")
# Destination: Zurich Hauptbahnhof (Zurich HB)
DEST_LAT = 47.3781
DEST_LNG = 8.5394
DEST_NAME = "Zurich HB"

# Simulated waypoints approaching Zurich HB from North-West at ~60 km/h (~16.6 m/s)
# Each step simulates 30 seconds of transit (~500 meters per step)
SIMULATED_ROUTE = [
    # (lat, lng, label)
    (47.4200, 8.4800, "Start of trip (~6.5 km away)"),
    (47.4100, 8.4950, "Approaching outer suburbs (~5.0 km away)"),
    (47.3980, 8.5120, "Entering city approach (~3.0 km away)"),
    (47.3870, 8.5280, "Approaching station corridor (~1.3 km away)"),
    (47.3830, 8.5340, "Inside Alert Zone (~700m away, ETA < 2 mins)"),
    (47.3810, 8.5370, "Final station approach (~350m away)"),
    (47.3785, 8.5390, "Platform arrival (~55m away)"),
]

# Waypoint for overshoot test (train continued past the station)
OVERSHOOT_WAYPOINT = (47.3680, 8.5450, "Moved past station (~1.2 km away)")


async def run_simulation():
    print("=" * 68)
    print(">>> TRAVEL BUDDY -- LIVE JOURNEY & PROXIMITY ALERT SIMULATOR")
    print("=" * 68)

    # Fresh temporary database (configured above) with a test user and trip
    await database.init_db()

    user_id = 9999
    remove_tracker(user_id)
    await database.upsert_user(user_id, "simulated_commuter")

    # Cancel any previous test trip
    await database.cancel_trip(user_id)

    # 1. Create a planned trip
    trip_id = await database.create_trip(
        user_id=user_id,
        destination_name=DEST_NAME,
        destination_lat=DEST_LAT,
        destination_lng=DEST_LNG,
        transport_mode="train",
    )
    trip = await database.get_active_or_planning_trip(user_id)
    assert trip["status"] == "planning"
    print(f"\n[OK] Planned Trip #{trip_id} created for destination '{DEST_NAME}'.")
    print(f"     Status: {trip['status']} | Destination GPS: {DEST_LAT}, {DEST_LNG}\n")

    alert_count = 0
    arrival_detected = False
    current_sim_time = time.time()

    print("-" * 68)
    print(f"{'Time':<8} | {'Event':<18} | {'Dist (km)':<10} | {'Speed':<10} | {'ETA (min)':<9}")
    print("-" * 68)

    for i, (lat, lng, label) in enumerate(SIMULATED_ROUTE):
        # Advance clock by 30 seconds for each ping
        current_sim_time += 30.0

        eval_result = process_location_ping(
            trip_dict=trip,
            current_lat=lat,
            current_lng=lng,
            timestamp=current_sim_time,
        )

        speed_str = f"{eval_result.speed_kmh:.1f} km/h" if eval_result.speed_kmh else "calc..."
        eta_str = f"{eval_result.eta_minutes:.1f}"

        print(
            f"+{i*30:>3}s   | {eval_result.event:<18} | {eval_result.distance_km:<10.2f} | {speed_str:<10} | {eta_str:<9} | {label}"
        )

        if eval_result.event == "TRACKING_STARTED":
            await database.activate_trip(trip_id, user_id)
            trip["status"] = "active"

        elif eval_result.event == "APPROACHING_ALERT":
            alert_count += 1
            await database.mark_alert_sent(trip_id)
            print(f"       >>> 🔔 PROXIMITY ALERT TRIGGERED! Stop is {eval_result.distance_km:.2f} km away (ETA ~{int(eval_result.eta_minutes)} min)")

        elif eval_result.event == "ARRIVED":
            arrival_detected = True
            await database.complete_trip(user_id)
            remove_tracker(user_id)
            print(f"       >>> 🎉 ARRIVAL DETECTED! Distance: {eval_result.distance_meters:.0f}m. Auto-marked as completed.")
            break

    # Assertions to verify correctness
    print("\n" + "=" * 68)
    print("VERIFICATION CHECKS:")
    print("=" * 68)

    assert alert_count == 1, f"Expected exactly 1 proximity alert, but got {alert_count}!"
    print("[OK] Duplicate alert check passed: Alert fired exactly once.")

    assert arrival_detected, "Expected arrival detection, but trip did not complete!"
    print("[OK] Auto-arrival completion check passed: Trip marked as completed.")

    user = await database.get_user(user_id)
    assert user["current_trip_status"] == "no_active_trip", "Expected user status to be reset to no_active_trip!"
    print("[OK] User status sync check passed: Reset to 'no_active_trip'.")

    # 2. Overshoot detection verification
    print("\n" + "-" * 68)
    print("TESTING DESTINATION OVERSHOOT DETECTION:")
    print("-" * 68)

    remove_tracker(user_id)
    await database.create_trip(
        user_id=user_id,
        destination_name=DEST_NAME,
        destination_lat=DEST_LAT,
        destination_lng=DEST_LNG,
        transport_mode="train",
    )
    trip2 = await database.get_active_or_planning_trip(user_id)

    t = time.time()
    # Ping 1: ~1.2 km away (Trip starts)
    e1 = process_location_ping(trip2, 47.3870, 8.5280, timestamp=t)
    print(f"Ping 1 (1.3 km): Event = {e1.event}")
    assert e1.event == "TRACKING_STARTED"

    # Ping 2: ~700m away (Fires approaching alert)
    e2 = process_location_ping(trip2, 47.3830, 8.5340, timestamp=t + 30)
    print(f"Ping 2 (700 m): Event = {e2.event}")
    assert e2.event == "APPROACHING_ALERT"

    # Ping 3: Closest approach at ~250m (Inside station zone, but doesn't reach 150m platform threshold)
    e3 = process_location_ping(trip2, 47.3800, 8.5380, timestamp=t + 60)
    print(f"Ping 3 (250 m): Event = {e3.event} (Min dist: {e3.distance_meters:.0f}m)")
    assert e3.event == "IN_TRANSIT"

    # Ping 4: Train continues past station to ~1.2 km away
    overshoot_eval = process_location_ping(
        trip2, OVERSHOOT_WAYPOINT[0], OVERSHOOT_WAYPOINT[1], timestamp=t + 120
    )
    print(f"Ping 4 (Past station): Event = {overshoot_eval.event} (Dist: {overshoot_eval.distance_km:.2f} km)")
    assert overshoot_eval.event == "OVERSHOT", f"Expected OVERSHOT event, got {overshoot_eval.event}"
    print("[OK] Overshoot detection check passed: User warned when moving away from stop.")

    # Cleanup
    await database.cancel_trip(user_id)
    remove_tracker(user_id)

    print("\n" + "=" * 68)
    print("✅ ALL PHASE 3 LIVE TRACKING & ALERTING TESTS PASSED!")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(run_simulation())
