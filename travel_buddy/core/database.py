"""
database.py — SQLite database setup and helper functions for Travel Buddy Bot.
Handles all DB operations: initialisation, users, and trips management.
Includes status synchronization and alert tracking for Phase 3 live tracking.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Stored in the project root by default; override with DATABASE_PATH in .env
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = os.getenv("DATABASE_PATH") or str(PROJECT_ROOT / "travel_bot.db")  # empty value = default


async def init_db() -> None:
    """
    Initialise the SQLite database and create tables if they don't exist.
    Enforces foreign keys and creates users and trips tables.
    Runs schema migrations if needed.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        # WAL lets the web portal read while the bot writes (no "database is locked" stalls).
        await db.execute("PRAGMA journal_mode = WAL;")

        # Users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id         INTEGER PRIMARY KEY,
                username            TEXT,
                current_trip_status TEXT    NOT NULL DEFAULT 'no_active_trip',
                created_at          TEXT    NOT NULL
            )
        """)

        # Trips table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                trip_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                destination_name    TEXT    NOT NULL,
                destination_lat     REAL    NOT NULL,
                destination_lng     REAL    NOT NULL,
                transport_mode      TEXT    NOT NULL CHECK (transport_mode IN ('train', 'bus')),
                status              TEXT    NOT NULL DEFAULT 'planning' 
                                            CHECK (status IN ('planning', 'active', 'completed', 'cancelled')),
                alert_sent          INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT    NOT NULL,
                completed_at        TEXT,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_trips_user ON trips (user_id, status)")

        # User preferences table (Phase 4)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                telegram_id         INTEGER PRIMARY KEY,
                interests           TEXT    NOT NULL,
                budget_level        TEXT    NOT NULL CHECK (budget_level IN ('low', 'medium', 'high')),
                pace                TEXT    NOT NULL CHECK (pace IN ('relaxed', 'packed')),
                updated_at          TEXT    NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
        """)

        # Migration: ensure alert_sent column exists if upgraded from Phase 2 DB
        try:
            await db.execute("ALTER TABLE trips ADD COLUMN alert_sent INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # Column already exists

        await db.commit()
        logger.info("Database initialised successfully with users, trips, and user_preferences tables.")


# ─────────────────────────────────────────────
#  User Queries
# ─────────────────────────────────────────────

async def get_user(telegram_id: int) -> dict | None:
    """
    Fetch a user record by telegram_id.
    Returns a dict if found, else None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_user(telegram_id: int, username: str | None) -> bool:
    """
    Insert a new user if they don't exist, or update username if changed.
    Returns True if a new user was inserted, False if they already existed.
    """
    existing = await get_user(telegram_id)
    if existing:
        if username and existing.get("username") != username:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
                await db.commit()
        return False  # already exists

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, username, current_trip_status, created_at)
            VALUES (?, ?, 'no_active_trip', ?)
            """,
            (telegram_id, username, datetime.utcnow().isoformat()),
        )
        await db.commit()
        logger.info("New user registered: telegram_id=%s, username=%s", telegram_id, username)
        return True


async def update_user_trip_status(telegram_id: int, status: str) -> None:
    """
    Update the current_trip_status for a given user in the users table.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET current_trip_status = ? WHERE telegram_id = ?",
            (status, telegram_id),
        )
        await db.commit()
        logger.info("User %s current_trip_status updated → %s", telegram_id, status)


# Backward compatibility alias
update_trip_status = update_user_trip_status


# ─────────────────────────────────────────────
#  Trip Queries & Lifecycle Management
# ─────────────────────────────────────────────

async def get_active_or_planning_trip(user_id: int) -> dict | None:
    """
    Fetch the ongoing trip for a user (status is either 'planning' or 'active').
    Returns the most recent matching trip dict, or None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM trips 
            WHERE user_id = ? AND status IN ('planning', 'active')
            ORDER BY trip_id DESC LIMIT 1
            """,
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_trip(
    user_id: int,
    destination_name: str,
    destination_lat: float,
    destination_lng: float,
    transport_mode: str,
) -> int:
    """
    Create a new trip with status 'planning', and sync users.current_trip_status = 'planning'.
    Returns the new trip_id.
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cursor = await db.execute(
            """
            INSERT INTO trips (
                user_id, destination_name, destination_lat, destination_lng,
                transport_mode, status, alert_sent, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'planning', 0, ?)
            """,
            (user_id, destination_name, destination_lat, destination_lng, transport_mode, now),
        )
        trip_id = cursor.lastrowid

        # Keep users.current_trip_status in sync
        await db.execute(
            "UPDATE users SET current_trip_status = 'planning' WHERE telegram_id = ?",
            (user_id,),
        )
        await db.commit()
        logger.info(
            "Created trip #%s for user %s to '%s' (%s). Status: planning",
            trip_id, user_id, destination_name, transport_mode
        )
        return trip_id


async def activate_trip(trip_id: int, user_id: int) -> None:
    """
    Transition trip from 'planning' to 'active' status when live location begins.
    Syncs users.current_trip_status to 'active'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE trips SET status = 'active' WHERE trip_id = ?",
            (trip_id,),
        )
        await db.execute(
            "UPDATE users SET current_trip_status = 'active' WHERE telegram_id = ?",
            (user_id,),
        )
        await db.commit()
        logger.info("Trip #%s transitioned to 'active' for user %s.", trip_id, user_id)


async def mark_alert_sent(trip_id: int) -> None:
    """
    Mark proximity alert as sent for this trip to prevent duplicate alerts.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE trips SET alert_sent = 1 WHERE trip_id = ?",
            (trip_id,),
        )
        await db.commit()
        logger.info("Trip #%s proximity alert marked as sent in DB.", trip_id)


async def complete_trip(user_id: int) -> dict | None:
    """
    Mark current ongoing trip ('planning' or 'active') as 'completed'.
    Sets completed_at timestamp and resets users.current_trip_status to 'no_active_trip'.
    Returns the updated trip record, or None if no active trip existed.
    """
    ongoing = await get_active_or_planning_trip(user_id)
    if not ongoing:
        return None

    trip_id = ongoing["trip_id"]
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE trips SET status = 'completed', completed_at = ? WHERE trip_id = ?",
            (now, trip_id),
        )
        await db.execute(
            "UPDATE users SET current_trip_status = 'no_active_trip' WHERE telegram_id = ?",
            (user_id,),
        )
        await db.commit()
        logger.info("Trip #%s marked as completed for user %s.", trip_id, user_id)

    ongoing["status"] = "completed"
    ongoing["completed_at"] = now
    return ongoing


async def cancel_trip(user_id: int) -> dict | None:
    """
    Mark current ongoing trip ('planning' or 'active') as 'cancelled'.
    Sets completed_at timestamp and resets users.current_trip_status to 'no_active_trip'.
    Returns the updated trip record, or None if no active trip existed.
    """
    ongoing = await get_active_or_planning_trip(user_id)
    if not ongoing:
        return None

    trip_id = ongoing["trip_id"]
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE trips SET status = 'cancelled', completed_at = ? WHERE trip_id = ?",
            (now, trip_id),
        )
        await db.execute(
            "UPDATE users SET current_trip_status = 'no_active_trip' WHERE telegram_id = ?",
            (user_id,),
        )
        await db.commit()
        logger.info("Trip #%s marked as cancelled for user %s.", trip_id, user_id)

    ongoing["status"] = "cancelled"
    ongoing["completed_at"] = now
    return ongoing


# ─────────────────────────────────────────────
#  User Preferences (Phase 4)
# ─────────────────────────────────────────────

async def get_user_preferences(telegram_id: int) -> dict | None:
    """
    Fetch user preferences by telegram_id. Returns dict or None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_preferences WHERE telegram_id = ?", (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_user_preferences(
    telegram_id: int, interests: str, budget_level: str, pace: str
) -> None:
    """
    Insert or update user preferences in user_preferences table.
    """
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_preferences (telegram_id, interests, budget_level, pace, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                interests = excluded.interests,
                budget_level = excluded.budget_level,
                pace = excluded.pace,
                updated_at = excluded.updated_at
            """,
            (telegram_id, interests, budget_level, pace, now),
        )
        await db.commit()
        logger.info(
            "Saved preferences for user %s: interests=%s, budget=%s, pace=%s",
            telegram_id, interests, budget_level, pace
        )

