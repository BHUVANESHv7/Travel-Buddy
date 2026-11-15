"""
geofence.py — Geo-fencing engine, emergency services lookup and emergency numbers.

  - Circular risk zones (safe / caution / high / restricted) stored in SQLite.
  - On every location ping we compute which zones the tourist is inside or approaching,
    and diff it against the previous state to produce ENTER / EXIT / APPROACH events.
  - Nearby police, hospitals, pharmacies and fire stations come from OpenStreetMap
    (Overpass API) — no API key needed.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from travel_buddy.core.tracker import haversine_distance_meters
from travel_buddy.core.usage import USER_AGENT, TTLCache, nominatim_slot, try_consume

logger = logging.getLogger(__name__)

APPROACH_BUFFER_M = 1000.0  # warn this far before the edge of high/restricted zones
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRRORS = [OVERPASS_URL, "https://overpass.private.coffee/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_BUDGET_S = 10.0  # total time allowed for Overpass before falling back to Nominatim

RISK_ORDER = {"safe": 0, "caution": 1, "high": 2, "restricted": 3}
RISK_EMOJI = {"safe": "🟢", "caution": "🟡", "high": "🟠", "restricted": "🔴"}


@dataclass
class ZoneEvent:
    kind: str  # "ENTER" | "EXIT" | "APPROACH"
    zone: dict
    distance_to_edge_m: float


def distance_to_zone_edge(lat: float, lng: float, zone: dict) -> float:
    """Negative when inside the zone, positive when outside."""
    d = haversine_distance_meters(lat, lng, zone["center_lat"], zone["center_lng"])
    return d - zone["radius_m"]


def zones_at(lat: float, lng: float, zones: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (zones the point is inside, high/restricted zones it is approaching)."""
    inside, near = [], []
    for z in zones:
        edge = distance_to_zone_edge(lat, lng, z)
        if edge <= 0:
            inside.append(z)
        elif edge <= APPROACH_BUFFER_M and RISK_ORDER[z["risk_level"]] >= RISK_ORDER["high"]:
            near.append(z)
    return inside, near


def evaluate_geofence(lat: float, lng: float, zones: list[dict],
                      previous_state: dict) -> tuple[list[ZoneEvent], dict]:
    """Diff current zones against the previous state. Returns (events, new_state)."""
    inside, near = zones_at(lat, lng, zones)
    prev_inside = set(previous_state.get("inside", []))
    prev_near = set(previous_state.get("near", []))
    by_id = {z["zone_id"]: z for z in zones}

    events: list[ZoneEvent] = []
    inside_ids = {z["zone_id"] for z in inside}
    near_ids = {z["zone_id"] for z in near}

    for z in inside:
        if z["zone_id"] not in prev_inside:
            events.append(ZoneEvent("ENTER", z, distance_to_zone_edge(lat, lng, z)))
    for zid in prev_inside - inside_ids:
        if zid in by_id:
            events.append(ZoneEvent("EXIT", by_id[zid], distance_to_zone_edge(lat, lng, by_id[zid])))
    for z in near:
        if z["zone_id"] not in prev_near and z["zone_id"] not in prev_inside:
            events.append(ZoneEvent("APPROACH", z, distance_to_zone_edge(lat, lng, z)))

    return events, {"inside": sorted(inside_ids), "near": sorted(near_ids)}


def highest_risk(zones: list[dict]) -> str:
    if not zones:
        return "safe"
    return max((z["risk_level"] for z in zones), key=lambda r: RISK_ORDER[r])


def zones_near(lat: float, lng: float, zones: list[dict], within_m: float = 50000) -> list[tuple[dict, float]]:
    out = []
    for z in zones:
        edge = distance_to_zone_edge(lat, lng, z)
        if edge <= within_m:
            out.append((z, edge))
    return sorted(out, key=lambda x: x[1])


# ─────────────────────────────────────────────
#  Emergency numbers (by ISO country code)
# ─────────────────────────────────────────────

EMERGENCY_NUMBERS = {
    "in": {"All emergencies": "112", "Police": "100", "Ambulance": "108", "Women helpline": "1091",
           "Tourist helpline (24x7, multilingual)": "1800-11-1363"},
    "us": {"All emergencies": "911"},
    "ca": {"All emergencies": "911"},
    "gb": {"All emergencies": "999 / 112", "Non-emergency police": "101"},
    "au": {"All emergencies": "000", "From mobile": "112"},
    "nz": {"All emergencies": "111"},
    "jp": {"Police": "110", "Fire / Ambulance": "119"},
    "cn": {"Police": "110", "Ambulance": "120", "Fire": "119"},
    "th": {"Tourist police": "1155", "Police": "191", "Ambulance": "1669"},
    "np": {"Police": "100", "Tourist police": "1144", "Ambulance": "102"},
    "lk": {"Police": "119", "Ambulance": "1990", "Tourist police": "1912"},
    "ae": {"Police": "999", "Ambulance": "998"},
    "sg": {"Police": "999", "Ambulance / Fire": "995"},
    "ch": {"All emergencies": "112", "Police": "117", "Ambulance": "144"},
}
DEFAULT_EMERGENCY = {"International emergency (GSM)": "112"}


def emergency_numbers_for(country_code: str | None) -> dict:
    if not country_code:
        return DEFAULT_EMERGENCY
    return EMERGENCY_NUMBERS.get(country_code.lower(), DEFAULT_EMERGENCY)


_reverse_cache = TTLCache(ttl_s=24 * 3600)
_services_cache = TTLCache(ttl_s=6 * 3600, max_items=500)
SERVICES_CACHE_RADIUS_M = 1000.0  # reuse a nearby-services lookup made within 1 km


async def reverse_geocode(lat: float, lng: float) -> dict | None:
    """Return {'display_name', 'country_code'} for a coordinate, or None."""
    key = (round(lat, 3), round(lng, 3))  # ~100 m grid
    cached = _reverse_cache.get(key)
    if cached:
        return cached
    params = {"lat": lat, "lon": lng, "format": "json", "zoom": 14}
    try:
        await nominatim_slot()  # OpenStreetMap policy: max 1 request per second
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(NOMINATIM_REVERSE_URL, params=params,
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
            r.raise_for_status()
            data = r.json()
            result = {
                "display_name": data.get("display_name", ""),
                "country_code": (data.get("address") or {}).get("country_code"),
            }
            _reverse_cache.set(key, result)
            return result
    except Exception as exc:
        logger.warning("Reverse geocoding failed for (%s, %s): %s", lat, lng, exc)
        return None


# ─────────────────────────────────────────────
#  Nearby emergency services (OpenStreetMap Overpass)
# ─────────────────────────────────────────────

SERVICE_LABELS = {
    "police": "👮 Police",
    "hospital": "🏥 Hospital",
    "clinic": "🩺 Clinic",
    "pharmacy": "💊 Pharmacy",
    "fire_station": "🚒 Fire station",
}


async def find_nearby_services(lat: float, lng: float, radius_m: int = 5000,
                               per_type: int = 2) -> list[dict]:
    """Return the closest emergency services, sorted by distance (cached ~1 km grid, 6 h)."""
    cache_key = (lat, lng, radius_m, per_type)
    cached = next((v for (c_lat, c_lng, c_r, c_n), v in _services_cache.items()
                   if c_r == radius_m and c_n == per_type
                   and haversine_distance_meters(lat, lng, c_lat, c_lng) <= SERVICES_CACHE_RADIUS_M), None)
    if cached is not None:  # a lookup within 1 km: same places, distances recomputed from here
        fresh = [{**c, "distance_m": haversine_distance_meters(lat, lng, c["lat"], c["lng"])} for c in cached]
        return sorted(fresh, key=lambda x: x["distance_m"])
    if not try_consume("places"):
        return []
    amenities = "|".join(SERVICE_LABELS)
    query = (
        f'[out:json][timeout:15];'
        f'(node["amenity"~"^({amenities})$"](around:{radius_m},{lat},{lng});'
        f' way["amenity"~"^({amenities})$"](around:{radius_m},{lat},{lng}););'
        f'out center 60;'
    )
    async def overpass() -> list | None:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for url in OVERPASS_MIRRORS:  # public servers are sometimes overloaded (502/504) — try the next
                try:
                    r = await client.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT})
                    r.raise_for_status()
                    return r.json().get("elements", [])
                except Exception as exc:
                    reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
                    logger.warning("Overpass lookup failed on %s: %s", url, reason)
        return None

    try:
        elements = await asyncio.wait_for(overpass(), timeout=OVERPASS_BUDGET_S)
    except asyncio.TimeoutError:
        logger.warning("Overpass took longer than %ss; using Nominatim fallback.", OVERPASS_BUDGET_S)
        elements = None
    if not elements:
        fallback = await _nominatim_services(lat, lng, radius_m, per_type)
        if fallback:
            _services_cache.set(cache_key, fallback)
        return fallback

    results = []
    for el in elements:
        tags = el.get("tags", {})
        e_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        e_lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if e_lat is None or e_lng is None:
            continue
        results.append({
            "type": tags.get("amenity"),
            "name": tags.get("name") or tags.get("name:en") or "Unnamed",
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "lat": e_lat,
            "lng": e_lng,
            "distance_m": haversine_distance_meters(lat, lng, e_lat, e_lng),
        })
    results.sort(key=lambda x: x["distance_m"])

    picked, counts = [], {}
    for item in results:
        if counts.get(item["type"], 0) < per_type:
            picked.append(item)
            counts[item["type"]] = counts.get(item["type"], 0) + 1
    if picked:
        _services_cache.set(cache_key, picked)
    return picked


async def _nominatim_services(lat: float, lng: float, radius_m: int, per_type: int) -> list[dict]:
    """Fallback: OpenStreetMap Nominatim search for police / hospitals / pharmacies near a point."""
    d = radius_m / 111_000  # degrees, roughly
    viewbox = f"{lng - d},{lat + d},{lng + d},{lat - d}"
    picked: list[dict] = []
    async with httpx.AsyncClient(timeout=8.0) as client:
        for i, kind in enumerate(("police", "hospital", "pharmacy")):
            try:
                await nominatim_slot()  # Nominatim usage policy: max 1 request per second
                r = await client.get(NOMINATIM_SEARCH_URL, params={
                    "q": kind, "format": "jsonv2", "limit": 6, "viewbox": viewbox, "bounded": 1,
                }, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
                r.raise_for_status()
                rows = r.json()
            except Exception as exc:
                logger.warning("Nominatim %s lookup failed: %s", kind, type(exc).__name__)
                continue
            found = []
            for row in rows:
                if row.get("category") != "amenity" or row.get("type") != kind:
                    continue
                e_lat, e_lng = float(row["lat"]), float(row["lon"])
                found.append({
                    "type": kind,
                    "name": row.get("name") or row.get("display_name", "Unnamed").split(",")[0],
                    "phone": None, "lat": e_lat, "lng": e_lng,
                    "distance_m": haversine_distance_meters(lat, lng, e_lat, e_lng),
                })
            picked += sorted(found, key=lambda x: x["distance_m"])[:per_type]
    return sorted(picked, key=lambda x: x["distance_m"])


def maps_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"
