"""
safety_db.py — SQLite schema & queries for the Tourist Safety layer.

Tables:
  - tourist_ids       Blockchain-anchored digital tourist IDs (no raw ID numbers stored)
  - ledger            Hash-chained, proof-of-work audit ledger (IDs, SOS, incidents)
  - guardians         Emergency contacts linked through Telegram deep-link invites
  - incidents         SOS / reported / auto-detected incidents and their lifecycle
  - geofence_zones    Risk zones (safe / caution / high / restricted)
  - safety_state      Per-user live safety state (last location, zones, check-ins, timers)
  - location_history  Short rolling trail of recent locations (for E-FIR & search)
"""

import json
import logging
import time
from datetime import datetime, timezone

import aiosqlite

from database import DB_PATH

logger = logging.getLogger(__name__)

LOCATION_HISTORY_LIMIT = 50  # rows kept per user

# Demo zones so geo-fencing works out of the box. Authorities add real ones with /addzone.
SEED_ZONES = [
    ("Kaziranga National Park (core area)", 26.5775, 93.1711, 15000, "restricted",
     "Protected wildlife area. Entry only with an authorised guide and permit; stay inside vehicles."),
    ("Nathu La Pass (permit area)", 27.3863, 88.8306, 5000, "restricted",
     "Border pass. Valid protected-area permit required; carry your ID at all times."),
    ("Cherrapunji cliffs & landslide stretch", 25.2702, 91.7323, 4000, "high",
     "Landslide and steep-cliff risk, especially in monsoon. Keep to marked paths; avoid after dark."),
    ("Tawang (Inner Line Permit area)", 27.5860, 91.8594, 8000, "caution",
     "Inner Line Permit required. High altitude; acclimatise and keep warm clothing."),
    ("Jaipur Old City market lanes", 26.9239, 75.8267, 1500, "caution",
     "Crowded markets: watch for pickpockets and over-charging. Use registered guides only."),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_safety_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tourist_ids (
                tourist_id       TEXT PRIMARY KEY,
                telegram_id      INTEGER NOT NULL,
                full_name        TEXT NOT NULL,
                nationality      TEXT NOT NULL,
                doc_type         TEXT NOT NULL,
                doc_hash         TEXT NOT NULL,
                doc_salt         TEXT NOT NULL,
                doc_last4        TEXT NOT NULL,
                emergency_name   TEXT,
                emergency_phone  TEXT,
                valid_from       TEXT NOT NULL,
                valid_until      TEXT NOT NULL,
                identity_hash    TEXT NOT NULL,
                block_hash       TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'revoked')),
                created_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tid_user ON tourist_ids (telegram_id);

            CREATE TABLE IF NOT EXISTS ledger (
                block_index    INTEGER PRIMARY KEY,
                timestamp      TEXT NOT NULL,
                block_type     TEXT NOT NULL,
                payload        TEXT NOT NULL,
                previous_hash  TEXT NOT NULL,
                nonce          INTEGER NOT NULL,
                hash           TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS guardians (
                guardian_row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                tourist_telegram_id  INTEGER NOT NULL,
                guardian_telegram_id INTEGER,
                guardian_name        TEXT,
                invite_code          TEXT NOT NULL UNIQUE,
                created_at           TEXT NOT NULL,
                linked_at            TEXT
            );

            CREATE TABLE IF NOT EXISTS incidents (
                incident_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id    INTEGER NOT NULL,
                incident_type  TEXT NOT NULL,
                severity       TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
                description    TEXT,
                lat            REAL,
                lng            REAL,
                status         TEXT NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open', 'acknowledged', 'resolved')),
                source         TEXT NOT NULL,
                ai_advice      TEXT,
                block_hash     TEXT,
                created_at     TEXT NOT NULL,
                acknowledged_by INTEGER,
                resolved_at    TEXT,
                resolved_by    INTEGER
            );

            CREATE TABLE IF NOT EXISTS geofence_zones (
                zone_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                center_lat  REAL NOT NULL,
                center_lng  REAL NOT NULL,
                radius_m    REAL NOT NULL,
                risk_level  TEXT NOT NULL CHECK (risk_level IN ('safe', 'caution', 'high', 'restricted')),
                advisory    TEXT,
                created_by  INTEGER,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS safety_state (
                telegram_id           INTEGER PRIMARY KEY,
                last_lat              REAL,
                last_lng              REAL,
                last_seen_ts          REAL,
                zone_state            TEXT NOT NULL DEFAULT '{"inside": [], "near": []}',
                checkin_pending_ts    REAL,
                checkin_reason        TEXT,
                inactivity_flagged    INTEGER NOT NULL DEFAULT 0,
                timer_due_ts          REAL,
                monitoring_enabled    INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS location_history (
                row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                lat          REAL NOT NULL,
                lng          REAL NOT NULL,
                ts           REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lochist_user ON location_history (telegram_id, ts);
        """)

        # Phase 6: evidence vault + extra per-user state (migrations are idempotent)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id   INTEGER,
                telegram_id   INTEGER NOT NULL,
                kind          TEXT NOT NULL,          -- photo | voice
                file_id       TEXT NOT NULL,
                sha256        TEXT NOT NULL,
                transcript    TEXT,
                block_hash    TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        for col, ddl in [
            ("hotspot_alerts", "TEXT NOT NULL DEFAULT '{}'"),
            ("weather_alert_key", "TEXT"),
            ("weather_checked_ts", "REAL"),
            ("community_alerts", "INTEGER NOT NULL DEFAULT 1"),
        ]:
            try:
                await db.execute(f"ALTER TABLE safety_state ADD COLUMN {col} {ddl}")
            except Exception:
                pass  # column already exists

        async with db.execute("SELECT COUNT(*) FROM geofence_zones") as cur:
            (zone_count,) = await cur.fetchone()
        if zone_count == 0:
            await db.executemany(
                """INSERT INTO geofence_zones
                   (name, center_lat, center_lng, radius_m, risk_level, advisory, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?)""",
                [(*z, now_iso()) for z in SEED_ZONES],
            )
            logger.info("Seeded %d demo geo-fence zones.", len(SEED_ZONES))
        await db.commit()
    logger.info("Safety tables initialised.")


# ─────────────────────────────────────────────
#  Generic helpers
# ─────────────────────────────────────────────

async def _fetchone(sql: str, params: tuple = ()) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _execute(sql: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


# ─────────────────────────────────────────────
#  Safety state
# ─────────────────────────────────────────────

async def get_safety_state(telegram_id: int) -> dict:
    row = await _fetchone("SELECT * FROM safety_state WHERE telegram_id = ?", (telegram_id,))
    if row is None:
        await _execute("INSERT OR IGNORE INTO safety_state (telegram_id) VALUES (?)", (telegram_id,))
        row = await _fetchone("SELECT * FROM safety_state WHERE telegram_id = ?", (telegram_id,))
    row["zone_state"] = json.loads(row.get("zone_state") or '{"inside": [], "near": []}')
    return row


async def update_safety_state(telegram_id: int, **fields) -> None:
    await get_safety_state(telegram_id)  # ensure row exists
    if "zone_state" in fields and not isinstance(fields["zone_state"], str):
        fields["zone_state"] = json.dumps(fields["zone_state"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    await _execute(
        f"UPDATE safety_state SET {cols} WHERE telegram_id = ?",
        (*fields.values(), telegram_id),
    )


async def get_all_safety_states() -> list[dict]:
    rows = await _fetchall("SELECT * FROM safety_state")
    for r in rows:
        r["zone_state"] = json.loads(r.get("zone_state") or '{"inside": [], "near": []}')
    return rows


async def record_location(telegram_id: int, lat: float, lng: float, ts: float | None = None) -> None:
    ts = ts or time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO location_history (telegram_id, lat, lng, ts) VALUES (?, ?, ?, ?)",
            (telegram_id, lat, lng, ts),
        )
        await db.execute(
            """DELETE FROM location_history WHERE telegram_id = ? AND row_id NOT IN (
                   SELECT row_id FROM location_history WHERE telegram_id = ?
                   ORDER BY ts DESC LIMIT ?)""",
            (telegram_id, telegram_id, LOCATION_HISTORY_LIMIT),
        )
        await db.commit()


async def get_location_history(telegram_id: int, limit: int = 10) -> list[dict]:
    return await _fetchall(
        "SELECT lat, lng, ts FROM location_history WHERE telegram_id = ? ORDER BY ts DESC LIMIT ?",
        (telegram_id, limit),
    )


# ─────────────────────────────────────────────
#  Geo-fence zones
# ─────────────────────────────────────────────

async def get_active_zones() -> list[dict]:
    return await _fetchall("SELECT * FROM geofence_zones WHERE active = 1")


async def add_zone(name: str, lat: float, lng: float, radius_m: float,
                   risk_level: str, advisory: str, created_by: int | None) -> int:
    return await _execute(
        """INSERT INTO geofence_zones
           (name, center_lat, center_lng, radius_m, risk_level, advisory, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, lat, lng, radius_m, risk_level, advisory, created_by, now_iso()),
    )


async def deactivate_zone(zone_id: int) -> None:
    await _execute("UPDATE geofence_zones SET active = 0 WHERE zone_id = ?", (zone_id,))


# ─────────────────────────────────────────────
#  Guardians
# ─────────────────────────────────────────────

async def create_guardian_invite(tourist_id: int, guardian_name: str, invite_code: str) -> int:
    return await _execute(
        """INSERT INTO guardians (tourist_telegram_id, guardian_name, invite_code, created_at)
           VALUES (?, ?, ?, ?)""",
        (tourist_id, guardian_name, invite_code, now_iso()),
    )


async def link_guardian(invite_code: str, guardian_telegram_id: int) -> dict | None:
    row = await _fetchone("SELECT * FROM guardians WHERE invite_code = ?", (invite_code,))
    if not row or row["guardian_telegram_id"] is not None:
        return None
    if row["tourist_telegram_id"] == guardian_telegram_id:
        return None  # cannot guard yourself
    await _execute(
        "UPDATE guardians SET guardian_telegram_id = ?, linked_at = ? WHERE invite_code = ?",
        (guardian_telegram_id, now_iso(), invite_code),
    )
    row["guardian_telegram_id"] = guardian_telegram_id
    return row


async def get_guardians(tourist_id: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM guardians WHERE tourist_telegram_id = ? ORDER BY guardian_row_id",
        (tourist_id,),
    )


async def get_linked_guardian_ids(tourist_id: int) -> list[int]:
    rows = await _fetchall(
        "SELECT guardian_telegram_id FROM guardians "
        "WHERE tourist_telegram_id = ? AND guardian_telegram_id IS NOT NULL",
        (tourist_id,),
    )
    return [r["guardian_telegram_id"] for r in rows]


async def is_guardian_of(guardian_id: int, tourist_id: int) -> bool:
    return tourist_id in await get_tourists_guarded_by(guardian_id)


async def get_tourists_guarded_by(guardian_id: int) -> list[int]:
    rows = await _fetchall(
        "SELECT tourist_telegram_id FROM guardians WHERE guardian_telegram_id = ?",
        (guardian_id,),
    )
    return [r["tourist_telegram_id"] for r in rows]


async def remove_guardian(tourist_id: int, guardian_row_id: int) -> bool:
    row = await _fetchone(
        "SELECT * FROM guardians WHERE guardian_row_id = ? AND tourist_telegram_id = ?",
        (guardian_row_id, tourist_id),
    )
    if not row:
        return False
    await _execute("DELETE FROM guardians WHERE guardian_row_id = ?", (guardian_row_id,))
    return True


# ─────────────────────────────────────────────
#  Incidents
# ─────────────────────────────────────────────

async def create_incident(telegram_id: int, incident_type: str, severity: str,
                          description: str, lat: float | None, lng: float | None,
                          source: str, ai_advice: str | None = None) -> int:
    return await _execute(
        """INSERT INTO incidents
           (telegram_id, incident_type, severity, description, lat, lng, status, source,
            ai_advice, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
        (telegram_id, incident_type, severity, description, lat, lng, source, ai_advice, now_iso()),
    )


async def set_incident_block_hash(incident_id: int, block_hash: str) -> None:
    await _execute("UPDATE incidents SET block_hash = ? WHERE incident_id = ?", (block_hash, incident_id))


async def set_incident_location(incident_id: int, lat: float, lng: float) -> None:
    await _execute("UPDATE incidents SET lat = ?, lng = ? WHERE incident_id = ?", (lat, lng, incident_id))


async def get_incident(incident_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))


async def get_user_incidents(telegram_id: int, limit: int = 10) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM incidents WHERE telegram_id = ? ORDER BY incident_id DESC LIMIT ?",
        (telegram_id, limit),
    )


async def get_open_incidents(limit: int = 20) -> list[dict]:
    return await _fetchall(
        """SELECT * FROM incidents WHERE status != 'resolved'
           ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END, incident_id DESC LIMIT ?""",
        (limit,),
    )


async def get_open_user_incidents(telegram_id: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM incidents WHERE telegram_id = ? AND status != 'resolved' ORDER BY incident_id DESC",
        (telegram_id,),
    )


async def acknowledge_incident(incident_id: int, by_user: int) -> None:
    await _execute(
        "UPDATE incidents SET status = 'acknowledged', acknowledged_by = ? "
        "WHERE incident_id = ? AND status = 'open'",
        (by_user, incident_id),
    )


async def resolve_incident(incident_id: int, by_user: int) -> None:
    await _execute(
        "UPDATE incidents SET status = 'resolved', resolved_at = ?, resolved_by = ? WHERE incident_id = ?",
        (now_iso(), by_user, incident_id),
    )


async def count_incidents_by_status() -> dict:
    rows = await _fetchall("SELECT status, severity, COUNT(*) AS n FROM incidents GROUP BY status, severity")
    return {(r["status"], r["severity"]): r["n"] for r in rows}


async def get_incidents_with_location(since_iso: str) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM incidents WHERE lat IS NOT NULL AND created_at >= ? "
        "AND incident_type NOT IN ('no_response', 'geofence_breach')",
        (since_iso,),
    )


async def get_users_seen_near(since_ts: float) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM safety_state WHERE last_lat IS NOT NULL AND last_seen_ts >= ?",
        (since_ts,),
    )


async def add_evidence(incident_id: int | None, telegram_id: int, kind: str, file_id: str,
                       sha256: str, transcript: str | None = None, block_hash: str | None = None) -> int:
    return await _execute(
        """INSERT INTO evidence (incident_id, telegram_id, kind, file_id, sha256, transcript, block_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (incident_id, telegram_id, kind, file_id, sha256, transcript, block_hash, now_iso()),
    )


async def get_evidence(incident_id: int) -> list[dict]:
    return await _fetchall("SELECT * FROM evidence WHERE incident_id = ? ORDER BY evidence_id", (incident_id,))


async def count_recent_incidents(telegram_id: int, since_iso: str) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM incidents WHERE telegram_id = ? AND created_at >= ?",
        (telegram_id, since_iso),
    )
    return row["n"] if row else 0
