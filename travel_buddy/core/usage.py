"""
usage.py — Keeps Travel Buddy at ₹0 / $0 running cost.

Every external service used is free, but some free tiers have daily limits or fair-use rules.
This module enforces them so the bot never needs a paid plan and never gets blocked:

  - Daily caps per service (AI calls, weather lookups, routing), persisted in SQLite so restarts
    don't reset them. When a cap is reached the feature quietly falls back to its offline mode.
  - A shared 1-request-per-second limiter for OpenStreetMap Nominatim (its usage policy).
  - Small TTL caches so repeated lookups for the same place don't hit the network again.
  - One honest User-Agent for all requests (required by OpenStreetMap policies).
"""

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone

from travel_buddy.config import env_int
from travel_buddy.core import database

logger = logging.getLogger(__name__)

USER_AGENT = "TravelBuddySafetyBot/2.0 (+https://github.com/BHUVANESHv7/Travel-Buddy)"

# Daily caps, kept well below each provider's free limits. Override in .env.
DAILY_LIMITS = {
    "ai": env_int("AI_DAILY_LIMIT", 300),            # Groq chat + voice calls
    "weather": env_int("WEATHER_DAILY_LIMIT", 5000),  # Open-Meteo (free < 10,000/day)
    "routing": env_int("ROUTING_DAILY_LIMIT", 500),   # OSRM public demo server
    "places": env_int("PLACES_DAILY_LIMIT", 2000),    # Overpass + Nominatim searches
}


# ─────────────────────────────────────────────
#  Daily counters (persisted)
# ─────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(database.DB_PATH, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS usage_counters ("
                "day TEXT NOT NULL, service TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (day, service))")
    return con


def used_today(service: str) -> int:
    try:
        con = _con()
        try:
            row = con.execute("SELECT count FROM usage_counters WHERE day = ? AND service = ?",
                              (_today(), service)).fetchone()
            return row[0] if row else 0
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def try_consume(service: str) -> bool:
    """Count one call. Returns False (and does not count) if today's free-tier cap is reached."""
    limit = DAILY_LIMITS.get(service)
    try:
        con = _con()
        try:
            day = _today()
            row = con.execute("SELECT count FROM usage_counters WHERE day = ? AND service = ?",
                              (day, service)).fetchone()
            count = row[0] if row else 0
            if limit is not None and count >= limit:
                if count == limit:  # log once
                    logger.warning("Daily free-tier cap reached for %s (%s). Using offline fallback until tomorrow.",
                                   service, limit)
                    con.execute("UPDATE usage_counters SET count = count + 1 WHERE day = ? AND service = ?",
                                (day, service))
                    con.commit()
                return False
            con.execute("INSERT INTO usage_counters (day, service, count) VALUES (?, ?, 1) "
                        "ON CONFLICT(day, service) DO UPDATE SET count = count + 1", (day, service))
            con.commit()
            return True
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.warning("Usage counter unavailable (%s); allowing call.", exc)
        return True


def usage_report() -> list[tuple[str, int, int]]:
    return [(s, min(used_today(s), lim), lim) for s, lim in DAILY_LIMITS.items()]


# ─────────────────────────────────────────────
#  OpenStreetMap Nominatim: max 1 request / second
# ─────────────────────────────────────────────

_locks: dict[int, asyncio.Lock] = {}
_last_nominatim = 0.0
NOMINATIM_INTERVAL_S = 1.1


async def nominatim_slot() -> None:
    """Await before every Nominatim request (shared across the whole bot)."""
    global _last_nominatim
    loop_id = id(asyncio.get_running_loop())
    lock = _locks.setdefault(loop_id, asyncio.Lock())
    async with lock:
        wait = _last_nominatim + NOMINATIM_INTERVAL_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_nominatim = time.monotonic()


# ─────────────────────────────────────────────
#  Tiny TTL cache
# ─────────────────────────────────────────────

class TTLCache:
    def __init__(self, ttl_s: float, max_items: int = 2000):
        self.ttl, self.max = ttl_s, max_items
        self._d: dict = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        self._d.pop(key, None)
        return None

    def items(self):
        """Live (non-expired) entries."""
        now = time.time()
        return [(k, v) for k, (t, v) in list(self._d.items()) if now - t < self.ttl]

    def set(self, key, value) -> None:
        if len(self._d) >= self.max:
            for k in sorted(self._d, key=lambda k: self._d[k][0])[: self.max // 4]:
                self._d.pop(k, None)
        self._d[key] = (time.time(), value)
