"""
geocoding.py — Geocoding helper service using OpenStreetMap Nominatim.

Why OpenStreetMap Nominatim?
1. Zero API Key Requirement: Works out of the box with zero external configuration or paid tokens.
2. Global Coverage: Resolves train stations, bus terminals, airports, landmarks, addresses, and cities worldwide.
3. Lightweight & Fast: Direct HTTP request via httpx (already bundled with python-telegram-bot).

Combined Approach:
The bot supports BOTH:
- Direct Location Pin / Venue sharing via Telegram's native attachment menu (exact GPS coordinates from device).
- Text Address Input: Users can simply type "Grand Central Station", "London Euston", or "Eiffel Tower" 
  and this service resolves the latitude, longitude, and formatted place name automatically.
"""

import logging

import httpx

from travel_buddy.core.usage import USER_AGENT, TTLCache, nominatim_slot

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_cache = TTLCache(ttl_s=7 * 86400)  # places don't move — avoid repeat lookups (free-tier friendly)


async def geocode_location(query: str) -> tuple[float, float, str] | None:
    """
    Convert a textual location or landmark into (latitude, longitude, display_name).
    Returns None if no matches are found or if the network request fails.
    """
    params = {
        "q": query.strip(),
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en",
    }

    key = query.strip().lower()
    cached = _cache.get(key)
    if cached:
        return cached

    try:
        await nominatim_slot()  # OpenStreetMap policy: max 1 request per second
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(NOMINATIM_URL, params=params, headers=headers)
            response.raise_for_status()
            results = response.json()

            if results and len(results) > 0:
                best_match = results[0]
                lat = float(best_match["lat"])
                lng = float(best_match["lon"])
                display_name = best_match.get("display_name", query)
                logger.info("Geocoded '%s' → (%s, %s): %s", query, lat, lng, display_name)
                _cache.set(key, (lat, lng, display_name))
                return lat, lng, display_name

            logger.warning("No geocoding results found for query: '%s'", query)
            return None

    except Exception as exc:
        logger.error("Error during geocoding query '%s': %s", query, exc)
        return None
