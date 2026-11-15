"""
safety_monitor.py — Behavioural anomaly detection & safety scoring.

Anomalies (evaluated on every location ping during an active trip):
  - ABRUPT_HALT      Was moving fast (>= 50 km/h) and suddenly became stationary for 3+ min,
                     away from the destination. Possible accident / breakdown.
  - PROLONGED_STOP   Stationary for 20+ min mid-journey, away from the destination.
  - ROUTE_DEVIATION  Distance to destination keeps growing (2+ km) before the stop was reached.

Inactivity (signal lost) and safety timers are handled by the background loop in
incident_service.py, which uses the same check-in mechanism.

Safety score (0–100) blends zone risk, time of day, open incidents, recent anomalies,
pending check-ins and preparedness (digital ID + guardian).
"""

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from tracker import haversine_distance_meters

STOP_RADIUS_M = 100.0
ABRUPT_HALT_MIN_SPEED_KMH = 50.0
ABRUPT_HALT_CONFIRM_S = 180.0
PROLONGED_STOP_S = 20 * 60.0
NEAR_DESTINATION_M = 1500.0
DEVIATION_WINDOW = 5
DEVIATION_GROWTH_M = 2000.0
JITTER_TOLERANCE_M = 30.0
ANOMALY_MEMORY_S = 3600.0


@dataclass
class Anomaly:
    kind: str
    severity: str  # low | medium | high
    message: str


@dataclass
class MotionState:
    trip_id: int | None = None
    last_ping: tuple | None = None  # (lat, lng, ts)
    anchor: tuple | None = None     # where the user became stationary
    speed_into_anchor: float = 0.0
    halt_alerted: bool = False
    stop_alerted: bool = False
    dest_distances: deque = field(default_factory=lambda: deque(maxlen=DEVIATION_WINDOW))
    deviation_warned_at: float | None = None
    anomaly_times: list = field(default_factory=list)


_motion_states: dict[int, MotionState] = {}


def reset_motion(user_id: int) -> None:
    _motion_states.pop(user_id, None)


def recent_anomaly_count(user_id: int, now: float | None = None) -> int:
    state = _motion_states.get(user_id)
    if not state:
        return 0
    now = now or time.time()
    state.anomaly_times = [t for t in state.anomaly_times if now - t < ANOMALY_MEMORY_S]
    return len(state.anomaly_times)


def analyze_motion(user_id: int, lat: float, lng: float, trip: dict | None,
                   alert_already_sent: bool = False, ts: float | None = None) -> list[Anomaly]:
    """Feed one location ping in, get any new anomalies out."""
    ts = ts or time.time()
    state = _motion_states.setdefault(user_id, MotionState())

    trip_id = trip["trip_id"] if trip else None
    if state.trip_id != trip_id:
        anomaly_times = state.anomaly_times
        state = MotionState(trip_id=trip_id, anomaly_times=anomaly_times)
        _motion_states[user_id] = state

    anomalies: list[Anomaly] = []
    prev = state.last_ping
    state.last_ping = (lat, lng, ts)

    if not trip or trip.get("status") not in ("active", "planning"):
        return anomalies

    dist_to_dest = haversine_distance_meters(lat, lng, trip["destination_lat"], trip["destination_lng"])
    away_from_destination = dist_to_dest > NEAR_DESTINATION_M

    # ── Stationary / abrupt halt detection ──
    if state.anchor is None:
        state.anchor = (lat, lng, ts)
    else:
        moved = haversine_distance_meters(lat, lng, state.anchor[0], state.anchor[1])
        if moved > STOP_RADIUS_M:
            seg_speed = 0.0
            if prev and ts > prev[2]:
                seg_speed = haversine_distance_meters(lat, lng, prev[0], prev[1]) / (ts - prev[2]) * 3.6
            state.anchor = (lat, lng, ts)
            state.speed_into_anchor = seg_speed
            state.halt_alerted = False
            state.stop_alerted = False
        else:
            stationary_s = ts - state.anchor[2]
            if (
                away_from_destination and not state.halt_alerted
                and state.speed_into_anchor >= ABRUPT_HALT_MIN_SPEED_KMH
                and stationary_s >= ABRUPT_HALT_CONFIRM_S
            ):
                state.halt_alerted = True
                state.stop_alerted = True  # one alert per stop
                anomalies.append(Anomaly(
                    "ABRUPT_HALT", "high",
                    f"Your vehicle stopped suddenly from ~{state.speed_into_anchor:.0f} km/h and has not "
                    f"moved for {stationary_s / 60:.0f} min.",
                ))
            elif away_from_destination and not state.stop_alerted and stationary_s >= PROLONGED_STOP_S:
                state.stop_alerted = True
                state.halt_alerted = True
                anomalies.append(Anomaly(
                    "PROLONGED_STOP", "medium",
                    f"You have been stationary for {stationary_s / 60:.0f} min, "
                    f"{dist_to_dest / 1000:.1f} km from your destination.",
                ))

    # ── Route deviation ──
    if not alert_already_sent:
        dists = state.dest_distances
        dists.append(dist_to_dest)
        if (
            len(dists) == DEVIATION_WINDOW
            and all(dists[i] > dists[i - 1] - JITTER_TOLERANCE_M for i in range(1, len(dists)))
            and dists[-1] - dists[0] >= DEVIATION_GROWTH_M
            and state.deviation_warned_at is None
        ):
            state.deviation_warned_at = dists[-1]
            anomalies.append(Anomaly(
                "ROUTE_DEVIATION", "medium",
                f"You are moving away from your destination (+{(dists[-1] - dists[0]) / 1000:.1f} km "
                f"over the last few updates).",
            ))
        elif state.deviation_warned_at is not None and dist_to_dest < state.deviation_warned_at - 1000:
            state.deviation_warned_at = None  # back on track; re-arm

    for _ in anomalies:
        state.anomaly_times.append(ts)
    return anomalies


# ─────────────────────────────────────────────
#  Safety score
# ─────────────────────────────────────────────

ZONE_PENALTY = {"safe": 0, "caution": 15, "high": 30, "restricted": 40}


def approx_local_hour(lng: float | None, now: datetime | None = None) -> float:
    """Solar-time approximation of the local hour from longitude (no timezone DB needed)."""
    now = now or datetime.now(timezone.utc)
    utc_hour = now.hour + now.minute / 60.0
    return (utc_hour + (lng or 0.0) / 15.0) % 24


def compute_safety_score(
    zone_risk: str = "safe",
    local_hour: float | None = None,
    open_incidents: int = 0,
    recent_anomalies: int = 0,
    checkin_pending: bool = False,
    has_digital_id: bool = True,
    has_guardian: bool = True,
    location_known: bool = True,
    weather_risk: str = "none",
    hotspot_risk: str | None = None,
) -> dict:
    score = 100
    factors = []

    def penalise(points: int, reason: str):
        nonlocal score
        if points:
            score -= points
            factors.append(f"-{points} {reason}")

    penalise(ZONE_PENALTY.get(zone_risk, 0), f"inside a {zone_risk}-risk zone")
    if local_hour is not None and (local_hour >= 22 or local_hour < 5):
        penalise(10, "late-night hours")
    penalise(min(open_incidents, 2) * 20, f"{open_incidents} open incident(s)")
    penalise(min(recent_anomalies, 2) * 15, f"{recent_anomalies} movement anomaly(ies) in the last hour")
    if checkin_pending:
        penalise(20, "safety check-in not answered")
    if not location_known:
        penalise(10, "no recent location")
    penalise({"high": 15, "medium": 5}.get(weather_risk, 0), f"{weather_risk}-risk weather")
    penalise({"high": 15, "caution": 8}.get(hotspot_risk or "", 0), "near a learned incident hotspot")
    if not has_digital_id:
        penalise(5, "no digital tourist ID")
    if not has_guardian:
        penalise(5, "no linked emergency contact")

    score = max(0, min(100, score))
    if score >= 75:
        label, emoji = "Safe", "🟢"
    elif score >= 50:
        label, emoji = "Moderate", "🟡"
    elif score >= 25:
        label, emoji = "Elevated risk", "🟠"
    else:
        label, emoji = "High risk", "🔴"
    return {"score": score, "label": label, "emoji": emoji, "factors": factors}
