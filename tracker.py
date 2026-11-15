"""
tracker.py — Live Location Tracking, Speed/ETA Estimation, and Proximity Alert Engine.

Mathematical Models:
1. Haversine Formula: Great-circle distance between coordinates on a spherical Earth (R=6371 km).
2. Speed Calculation: Smoothed moving average across a sliding window of recent location pings (3-5 pings).
3. ETA Calculation: Remaining distance / current speed. If vehicle is temporarily stopped (e.g. signal),
   it falls back to mode-specific average speeds (Train: 45-50 km/h, Bus: 25 km/h).
4. Hybrid Proximity Alert Trigger:
   - Primary: ETA <= 4.0 minutes (essential for high-speed trains where 1 km passes in 30-40 seconds).
   - Secondary: Distance <= 1.0 km (essential failsafe for slow city traffic or station approaches).
5. Auto-Arrival Detection:
   - Distance <= 150 meters (auto-completes the trip and notifies passenger).
6. Overshoot Detection:
   - If distance increases significantly after reaching closest approach without auto-completing,
     advises the user that their stop was likely passed.
"""

import math
import time
import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Configurable Alert & Tracking Thresholds
# ─────────────────────────────────────────────

ALERT_ETA_MINUTES_THRESHOLD = 4.0        # Trigger approaching alert when ETA <= 4 minutes
ALERT_DISTANCE_KM_THRESHOLD = 1.0        # Trigger approaching alert when distance <= 1.0 km
ARRIVAL_DISTANCE_METERS_THRESHOLD = 150.0 # Auto-complete trip when distance <= 150m

MAX_PINGS_HISTORY = 5                    # Number of recent pings kept for speed smoothing
MIN_SPEED_KMH = 3.0                      # Speeds below 3 km/h treated as stationary/stopped
MAX_REASONABLE_SPEED_KMH = 350.0         # Reject extreme GPS teleportation spikes (>350 km/h)
DEFAULT_TRAIN_SPEED_KMH = 45.0           # Default train speed used when stationary
DEFAULT_BUS_SPEED_KMH = 25.0             # Default bus speed used when stationary
OVERSHOOT_BUFFER_METERS = 500.0          # Distance growth past closest approach to trigger overshoot warning


# ─────────────────────────────────────────────
#  Data Structures
# ─────────────────────────────────────────────

@dataclass
class LocationPing:
    lat: float
    lng: float
    timestamp: float  # Unix epoch seconds


@dataclass
class TripTrackingState:
    trip_id: int
    user_id: int
    destination_name: str
    destination_lat: float
    destination_lng: float
    transport_mode: str
    status: str
    alert_sent: bool = False
    overshoot_warned: bool = False
    min_distance_meters: float = float("inf")
    pings: deque = field(default_factory=lambda: deque(maxlen=MAX_PINGS_HISTORY))


@dataclass
class TrackingEvaluation:
    event: str  # "TRACKING_STARTED", "IN_TRANSIT", "APPROACHING_ALERT", "ARRIVED", "OVERSHOT"
    distance_meters: float
    distance_km: float
    speed_kmh: float | None
    eta_minutes: float
    eta_basis: str  # "live_speed" | "average_speed"
    destination_name: str
    trip_id: int
    message: str = ""


# In-memory registry for active trip tracking states
_active_trackers: dict[int, TripTrackingState] = {}


# ─────────────────────────────────────────────
#  Distance & Geodesic Math
# ─────────────────────────────────────────────

def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points in meters using the Haversine formula.
    """
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


# ─────────────────────────────────────────────
#  Speed & ETA Estimation
# ─────────────────────────────────────────────

def calculate_speed_kmh(pings: deque) -> float | None:
    """
    Compute smoothed ground speed (km/h) across the stored window of location pings.
    Returns None if insufficient data or if elapsed time is too short.
    """
    if len(pings) < 2:
        return None

    pings_list = list(pings)
    total_dist_meters = 0.0
    for i in range(1, len(pings_list)):
        p1 = pings_list[i - 1]
        p2 = pings_list[i]
        total_dist_meters += haversine_distance_meters(p1.lat, p1.lng, p2.lat, p2.lng)

    total_time_seconds = pings_list[-1].timestamp - pings_list[0].timestamp
    if total_time_seconds < 1.0:
        return None

    speed_mps = total_dist_meters / total_time_seconds
    speed_kmh = speed_mps * 3.6

    # Jitter / noise filter
    if speed_kmh > MAX_REASONABLE_SPEED_KMH:
        logger.warning("Unrealistic speed detected (%.1f km/h) — ignoring jitter spike", speed_kmh)
        return None

    return speed_kmh


def calculate_eta_minutes(
    remaining_dist_meters: float,
    current_speed_kmh: float | None,
    transport_mode: str,
) -> tuple[float, str]:
    """
    Calculate ETA in minutes and return (eta_minutes, eta_basis).
    Uses live speed if available and above MIN_SPEED_KMH; otherwise falls back
    to typical transport mode travel speeds.
    """
    dist_km = remaining_dist_meters / 1000.0

    if current_speed_kmh is not None and current_speed_kmh >= MIN_SPEED_KMH:
        eta_hours = dist_km / current_speed_kmh
        return (max(0.1, eta_hours * 60.0), "live_speed")

    fallback_speed = (
        DEFAULT_TRAIN_SPEED_KMH if transport_mode.lower() == "train" else DEFAULT_BUS_SPEED_KMH
    )
    eta_hours = dist_km / fallback_speed
    return (max(0.1, eta_hours * 60.0), "average_speed")


# ─────────────────────────────────────────────
#  Tracking State Management
# ─────────────────────────────────────────────

def get_or_create_tracker(trip_dict: dict) -> TripTrackingState:
    """
    Get existing tracking state from memory or initialize a new one from a DB trip record.
    """
    user_id = trip_dict["user_id"]
    trip_id = trip_dict["trip_id"]

    if user_id in _active_trackers and _active_trackers[user_id].trip_id == trip_id:
        return _active_trackers[user_id]

    state = TripTrackingState(
        trip_id=trip_id,
        user_id=user_id,
        destination_name=trip_dict["destination_name"],
        destination_lat=float(trip_dict["destination_lat"]),
        destination_lng=float(trip_dict["destination_lng"]),
        transport_mode=trip_dict.get("transport_mode", "train"),
        status=trip_dict.get("status", "planning"),
        alert_sent=bool(trip_dict.get("alert_sent", 0)),
    )
    _active_trackers[user_id] = state
    return state


def remove_tracker(user_id: int) -> None:
    """
    Remove tracker from active memory when trip completes or cancels.
    """
    _active_trackers.pop(user_id, None)


# ─────────────────────────────────────────────
#  Core Evaluation Pipeline
# ─────────────────────────────────────────────

def process_location_ping(
    trip_dict: dict,
    current_lat: float,
    current_lng: float,
    timestamp: float | None = None,
) -> TrackingEvaluation:
    """
    Process a new location update for an active/planning trip.
    Evaluates:
      - State transition (planning -> active)
      - Speed and ETA
      - Auto-arrival completion (distance <= 150m)
      - Proximity alert (ETA <= 4 min OR distance <= 1km)
      - Overshoot detection
    """
    if timestamp is None:
        timestamp = time.time()

    state = get_or_create_tracker(trip_dict)

    # 1. Check if trip is transitioning from planning -> active
    # A fresh tracker (no pings yet) also counts as a start: the handler may already
    # have flipped the DB status to 'active' before calling us.
    was_planning = (state.status == "planning") or len(state.pings) == 0
    if was_planning:
        state.status = "active"

    # 2. Append ping to sliding window
    state.pings.append(LocationPing(lat=current_lat, lng=current_lng, timestamp=timestamp))

    # 3. Calculate distance to destination
    dist_meters = haversine_distance_meters(
        current_lat, current_lng, state.destination_lat, state.destination_lng
    )
    dist_km = dist_meters / 1000.0

    # 4. Update closest approach distance
    if dist_meters < state.min_distance_meters:
        state.min_distance_meters = dist_meters

    # 5. Estimate speed & ETA
    speed_kmh = calculate_speed_kmh(state.pings)
    eta_minutes, eta_basis = calculate_eta_minutes(dist_meters, speed_kmh, state.transport_mode)

    # 6. Check Arrival condition (Highest priority)
    if dist_meters <= ARRIVAL_DISTANCE_METERS_THRESHOLD:
        state.status = "completed"
        return TrackingEvaluation(
            event="ARRIVED",
            distance_meters=dist_meters,
            distance_km=dist_km,
            speed_kmh=speed_kmh,
            eta_minutes=eta_minutes,
            eta_basis=eta_basis,
            destination_name=state.destination_name,
            trip_id=state.trip_id,
            message=f"Arrived at destination ({dist_meters:.0f}m remaining).",
        )

    # 7. Check if trip just started tracking
    if was_planning and len(state.pings) == 1:
        return TrackingEvaluation(
            event="TRACKING_STARTED",
            distance_meters=dist_meters,
            distance_km=dist_km,
            speed_kmh=speed_kmh,
            eta_minutes=eta_minutes,
            eta_basis=eta_basis,
            destination_name=state.destination_name,
            trip_id=state.trip_id,
            message=f"Live journey tracking initiated towards {state.destination_name}.",
        )

    # 8. Check Proximity Alert condition
    # Trigger if ETA <= 4 minutes OR distance <= 1.0 km
    should_alert = (
        (eta_minutes <= ALERT_ETA_MINUTES_THRESHOLD or dist_km <= ALERT_DISTANCE_KM_THRESHOLD)
        and not state.alert_sent
    )
    if should_alert:
        state.alert_sent = True
        return TrackingEvaluation(
            event="APPROACHING_ALERT",
            distance_meters=dist_meters,
            distance_km=dist_km,
            speed_kmh=speed_kmh,
            eta_minutes=eta_minutes,
            eta_basis=eta_basis,
            destination_name=state.destination_name,
            trip_id=state.trip_id,
            message="Approaching destination stop.",
        )

    # 9. Check Overshoot condition
    # If alert was already sent, user was previously close (< 400m), and distance is now increasing past closest approach
    if (
        state.alert_sent
        and not state.overshoot_warned
        and state.min_distance_meters < 400.0
        and dist_meters > (state.min_distance_meters + OVERSHOOT_BUFFER_METERS)
        and dist_meters > 600.0
    ):
        state.overshoot_warned = True
        return TrackingEvaluation(
            event="OVERSHOT",
            distance_meters=dist_meters,
            distance_km=dist_km,
            speed_kmh=speed_kmh,
            eta_minutes=eta_minutes,
            eta_basis=eta_basis,
            destination_name=state.destination_name,
            trip_id=state.trip_id,
            message="Possible destination overshoot detected.",
        )

    # 10. Normal in-transit location ping
    return TrackingEvaluation(
        event="IN_TRANSIT",
        distance_meters=dist_meters,
        distance_km=dist_km,
        speed_kmh=speed_kmh,
        eta_minutes=eta_minutes,
        eta_basis=eta_basis,
        destination_name=state.destination_name,
        trip_id=state.trip_id,
    )
