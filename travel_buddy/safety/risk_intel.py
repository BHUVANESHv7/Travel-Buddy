"""
risk_intel.py — Predictive risk intelligence.

1. Self-learning hotspots
   Incidents with coordinates from the last HOTSPOT_DAYS are clustered (density clustering,
   DBSCAN-style) into "dynamic geo-fences". Each hotspot gets a recency- and severity-weighted
   risk score, so the map of danger updates itself as tourists report incidents.

2. Weather & natural-hazard awareness (Open-Meteo, free, no API key)
   Thunderstorms, heavy rain/snow, fog, extreme heat/cold and strong gusts — now and next 6 hours.

3. Safe-route analysis
   Real road route from OSRM (fallback: straight line), sampled every ~250 m and checked against
   geo-fence zones, learned hotspots, destination weather and time of day → verdict + risk score.
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import httpx

from travel_buddy.safety.geofence import RISK_ORDER, distance_to_zone_edge
from travel_buddy.core.tracker import haversine_distance_meters

logger = logging.getLogger(__name__)

USER_AGENT = "TravelBuddyTelegramBot/1.0 (https://github.com/travelbuddy)"

HOTSPOT_DAYS = 30
HOTSPOT_EPS_M = 500.0       # neighbourhood radius for clustering
HOTSPOT_MIN_POINTS = 2      # incidents needed to form a hotspot
HOTSPOT_WARN_M = 300.0      # warn when this close to a hotspot edge
SEVERITY_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 3.0, "critical": 4.0}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{a_lng},{a_lat};{b_lng},{b_lat}"


# ═════════════════════════════════════════════
#  1. Self-learning hotspots
# ═════════════════════════════════════════════

def _incident_age_days(inc: dict, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(inc["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (now - created).total_seconds() / 86400)
    except Exception:
        return HOTSPOT_DAYS


def cluster_hotspots(incidents: list[dict], eps_m: float = HOTSPOT_EPS_M,
                     min_points: int = HOTSPOT_MIN_POINTS, now: datetime | None = None) -> list[dict]:
    """Density clustering of incident locations. Returns hotspots sorted by risk score."""
    now = now or datetime.now(timezone.utc)
    pts = [i for i in incidents if i.get("lat") is not None and i.get("lng") is not None]
    n = len(pts)
    neighbours = [
        [j for j in range(n)
         if haversine_distance_meters(pts[i]["lat"], pts[i]["lng"], pts[j]["lat"], pts[j]["lng"]) <= eps_m]
        for i in range(n)
    ]
    labels = [None] * n
    cluster_id = 0
    for i in range(n):
        if labels[i] is not None or len(neighbours[i]) < min_points:
            continue
        labels[i] = cluster_id
        queue = list(neighbours[i])
        while queue:
            j = queue.pop()
            if labels[j] is None:
                labels[j] = cluster_id
                if len(neighbours[j]) >= min_points:
                    queue.extend(k for k in neighbours[j] if labels[k] is None)
        cluster_id += 1

    hotspots = []
    for c in range(cluster_id):
        members = [pts[i] for i in range(n) if labels[i] == c]
        lat = sum(m["lat"] for m in members) / len(members)
        lng = sum(m["lng"] for m in members) / len(members)
        radius = max(150.0, max(haversine_distance_meters(lat, lng, m["lat"], m["lng"]) for m in members) + 100)
        score = 0.0
        types: dict[str, int] = {}
        for m in members:
            recency = math.exp(-_incident_age_days(m, now) / 10.0)  # ~10-day half-life-ish decay
            score += SEVERITY_WEIGHT.get(m.get("severity"), 1.0) * (0.3 + 0.7 * recency)
            types[m["incident_type"]] = types.get(m["incident_type"], 0) + 1
        risk = "high" if score >= 8 else "caution" if score >= 3 else "safe"
        hotspots.append({
            "hotspot_id": f"H{c + 1}",
            "lat": lat, "lng": lng, "radius_m": radius,
            "count": len(members),
            "score": round(score, 1),
            "risk_level": risk,
            "dominant_type": max(types, key=types.get),
            "types": types,
        })
    return sorted(hotspots, key=lambda h: -h["score"])


_hotspot_cache: dict = {"ts": 0.0, "data": []}
HOTSPOT_CACHE_S = 300


async def get_hotspots(force: bool = False) -> list[dict]:
    from travel_buddy.safety.store import get_incidents_with_location  # local import avoids cycles
    if not force and time.time() - _hotspot_cache["ts"] < HOTSPOT_CACHE_S:
        return _hotspot_cache["data"]
    since = (datetime.now(timezone.utc) - timedelta(days=HOTSPOT_DAYS)).isoformat()
    data = cluster_hotspots(await get_incidents_with_location(since))
    _hotspot_cache.update(ts=time.time(), data=data)
    return data


def invalidate_hotspots() -> None:
    _hotspot_cache["ts"] = 0.0


def hotspots_near(lat: float, lng: float, hotspots: list[dict], within_m: float) -> list[tuple[dict, float]]:
    out = []
    for h in hotspots:
        if h["risk_level"] == "safe":
            continue
        edge = haversine_distance_meters(lat, lng, h["lat"], h["lng"]) - h["radius_m"]
        if edge <= within_m:
            out.append((h, edge))
    return sorted(out, key=lambda x: x[1])


# ═════════════════════════════════════════════
#  2. Weather & natural hazards
# ═════════════════════════════════════════════

WMO_TEXT = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "rain showers", 81: "heavy showers", 82: "violent showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}

_weather_cache: dict[tuple, tuple[float, dict]] = {}
WEATHER_CACHE_S = 900


def assess_weather(data: dict) -> dict:
    """Turn an Open-Meteo response into hazards. Pure function (testable offline)."""
    cur = data.get("current", {})
    hourly = data.get("hourly", {})
    codes = [cur.get("weather_code")] + list(hourly.get("weather_code") or [])
    gusts = [cur.get("wind_gusts_10m") or 0] + list(hourly.get("wind_gusts_10m") or [])
    rain = [cur.get("precipitation") or 0] + list(hourly.get("precipitation") or [])
    temps = [cur.get("apparent_temperature", cur.get("temperature_2m"))] + list(hourly.get("temperature_2m") or [])
    codes = [c for c in codes if c is not None]
    temps = [t for t in temps if t is not None]

    hazards = []

    def add(key, severity, text):
        hazards.append({"key": key, "severity": severity, "text": text})

    if any(c >= 95 for c in codes):
        add("thunderstorm", "high", "⛈️ Thunderstorm expected — avoid open ground, hilltops, water and isolated trees.")
    if any(c in (65, 67, 82) for c in codes) or max(rain or [0]) >= 7.5:
        add("heavy_rain", "high", "🌧️ Heavy rain — risk of flash floods and landslides on hill roads; avoid river banks.")
    if any(c in (75, 86) for c in codes):
        add("heavy_snow", "high", "❄️ Heavy snow — roads and passes may close; carry warm layers and don't drive at night.")
    if max(gusts or [0]) >= 60:
        add("wind", "high" if max(gusts) >= 80 else "medium",
            f"💨 Strong wind gusts up to {max(gusts):.0f} km/h — beware of falling branches and boats.")
    if any(c in (45, 48) for c in codes):
        add("fog", "medium", "🌫️ Fog — low visibility on roads; avoid night driving and trekking.")
    if temps and max(temps) >= 40:
        add("heat", "high", f"🥵 Extreme heat (feels like {max(temps):.0f}°C) — hydrate, avoid 12–4 pm outdoors.")
    elif temps and max(temps) >= 35:
        add("heat", "medium", f"☀️ Hot (feels like {max(temps):.0f}°C) — carry water and sun protection.")
    if temps and min(temps) <= -5:
        add("cold", "medium", f"🥶 Severe cold (down to {min(temps):.0f}°C) — risk of hypothermia; dress in layers.")

    worst = max((h["severity"] for h in hazards), key=lambda s: {"low": 0, "medium": 1, "high": 2}[s], default="none")
    return {
        "summary": f"{WMO_TEXT.get(cur.get('weather_code'), 'unknown')}, {cur.get('temperature_2m', '?')}°C",
        "temperature": cur.get("temperature_2m"),
        "hazards": hazards,
        "worst": worst,
        "timezone": data.get("timezone"),
        "utc_offset_seconds": data.get("utc_offset_seconds"),
    }


async def get_weather_risk(lat: float, lng: float) -> dict | None:
    key = (round(lat, 2), round(lng, 2))
    hit = _weather_cache.get(key)
    if hit and time.time() - hit[0] < WEATHER_CACHE_S:
        return hit[1]
    params = {
        "latitude": lat, "longitude": lng,
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_gusts_10m",
        "hourly": "precipitation,weather_code,wind_gusts_10m,temperature_2m",
        "forecast_hours": 6, "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(OPEN_METEO_URL, params=params, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            result = assess_weather(r.json())
    except Exception as exc:
        logger.warning("Weather lookup failed for (%s, %s): %s", lat, lng, exc)
        return None
    _weather_cache[key] = (time.time(), result)
    return result


# ═════════════════════════════════════════════
#  3. Safe-route analysis
# ═════════════════════════════════════════════

def interpolate_line(a: tuple[float, float], b: tuple[float, float], step_m: float = 250.0) -> list[tuple[float, float]]:
    d = haversine_distance_meters(a[0], a[1], b[0], b[1])
    n = max(1, int(d // step_m))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def densify(path: list[tuple[float, float]], step_m: float = 250.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in zip(path, path[1:]):
        seg = interpolate_line(a, b, step_m)
        out.extend(seg if not out else seg[1:])
    return out or path


async def fetch_route(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> dict:
    """Road route via OSRM; falls back to the straight line."""
    url = OSRM_URL.format(a_lat=a_lat, a_lng=a_lng, b_lat=b_lat, b_lng=b_lng)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params={"overview": "full", "geometries": "geojson"},
                                 headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            route = r.json()["routes"][0]
            path = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]
            return {"source": "road", "path": path, "distance_m": route["distance"], "duration_s": route["duration"]}
    except Exception as exc:
        logger.warning("OSRM routing failed, using straight line: %s", exc)
        dist = haversine_distance_meters(a_lat, a_lng, b_lat, b_lng)
        return {"source": "straight", "path": [(a_lat, a_lng), (b_lat, b_lng)],
                "distance_m": dist, "duration_s": dist / (40 / 3.6)}


def assess_route(path: list[tuple[float, float]], zones: list[dict], hotspots: list[dict],
                 local_hour: float | None = None, weather: dict | None = None) -> dict:
    """Score a route. Pure function (testable offline)."""
    samples = densify(path)
    zone_hits: dict[int, dict] = {}
    for z in zones:
        if RISK_ORDER[z["risk_level"]] < RISK_ORDER["caution"]:
            continue
        for lat, lng in samples:
            if distance_to_zone_edge(lat, lng, z) <= 0:
                zone_hits[z["zone_id"]] = z
                break
    hot_hits: dict[str, dict] = {}
    for h in hotspots:
        if h["risk_level"] == "safe":
            continue
        for lat, lng in samples:
            if haversine_distance_meters(lat, lng, h["lat"], h["lng"]) <= h["radius_m"]:
                hot_hits[h["hotspot_id"]] = h
                break

    score = 100
    reasons = []
    for z in zone_hits.values():
        pen = {"caution": 10, "high": 25, "restricted": 45}[z["risk_level"]]
        score -= pen
        reasons.append(f"passes through {z['risk_level']} zone '{z['name']}' (-{pen})")
    for h in hot_hits.values():
        pen = 20 if h["risk_level"] == "high" else 10
        score -= pen
        reasons.append(f"crosses incident hotspot {h['hotspot_id']} ({h['count']} recent {h['dominant_type']} "
                       f"reports) (-{pen})")
    if local_hour is not None and (local_hour >= 22 or local_hour < 5):
        score -= 15
        reasons.append("travelling late at night (-15)")
    if weather and weather.get("worst") in ("medium", "high"):
        pen = 20 if weather["worst"] == "high" else 8
        score -= pen
        reasons.append(f"weather: {', '.join(h['key'].replace('_', ' ') for h in weather['hazards'])} (-{pen})")

    score = max(0, score)
    if any(z["risk_level"] == "restricted" for z in zone_hits.values()):
        verdict = "⛔ Not recommended — route enters a restricted area (permit / authorised guide needed)."
    elif score >= 75:
        verdict = "✅ Looks safe — normal precautions."
    elif score >= 50:
        verdict = "⚠️ Go with care — stay alert on the flagged stretches."
    else:
        verdict = "🛑 High risk — consider another time, route or a registered guide/taxi."
    return {"score": score, "verdict": verdict, "reasons": reasons,
            "zones": list(zone_hits.values()), "hotspots": list(hot_hits.values())}


def directions_link(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> str:
    return (f"https://www.google.com/maps/dir/?api=1&origin={a_lat:.6f},{a_lng:.6f}"
            f"&destination={b_lat:.6f},{b_lng:.6f}")
