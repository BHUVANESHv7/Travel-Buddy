"""
blockchain_id.py — Blockchain-based Digital Tourist ID & tamper-evident audit ledger.

How it works:
  - Every important safety event (ID issued/revoked, SOS, incident reported/resolved) is
    written as a block. Each block stores the SHA-256 hash of the previous block, so
    changing any old block breaks every hash after it.
  - Blocks are "mined" with a light proof-of-work (hash must start with DIFFICULTY zeros),
    which makes rewriting history expensive.
  - Privacy by design: blocks hold only hashes and IDs, never names or document numbers.
    The ID document number itself is never stored — only a salted hash and the last 4 chars.
  - Verification recomputes the identity hash from the database record and compares it
    with the one sealed in the ledger, so any edit to a tourist's record is detected.
"""

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite

from database import DB_PATH
from safety_db import _fetchone, _fetchall, _execute, now_iso

logger = logging.getLogger(__name__)

DIFFICULTY = 3  # number of leading hex zeros required in a block hash
GENESIS_PREV_HASH = "0" * 64

_ledger_lock = asyncio.Lock()


# ─────────────────────────────────────────────
#  Hashing & mining
# ─────────────────────────────────────────────

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_block_hash(index: int, timestamp: str, block_type: str,
                       payload_json: str, previous_hash: str, nonce: int) -> str:
    return sha256(f"{index}|{timestamp}|{block_type}|{payload_json}|{previous_hash}|{nonce}")


def mine_block(index: int, timestamp: str, block_type: str,
               payload_json: str, previous_hash: str) -> tuple[int, str]:
    prefix = "0" * DIFFICULTY
    nonce = 0
    while True:
        h = compute_block_hash(index, timestamp, block_type, payload_json, previous_hash, nonce)
        if h.startswith(prefix):
            return nonce, h
        nonce += 1


# ─────────────────────────────────────────────
#  Ledger operations
# ─────────────────────────────────────────────

async def _last_block() -> dict | None:
    return await _fetchone("SELECT * FROM ledger ORDER BY block_index DESC LIMIT 1")


async def append_block(block_type: str, payload: dict) -> dict:
    """Mine and append a new block. Returns the stored block as a dict."""
    async with _ledger_lock:
        last = await _last_block()
        if last is None:
            await _write_block(0, "GENESIS", {"network": "TravelBuddy-SafetyChain"}, GENESIS_PREV_HASH)
            last = await _last_block()
        return await _write_block(last["block_index"] + 1, block_type, payload, last["hash"])


async def _write_block(index: int, block_type: str, payload: dict, previous_hash: str) -> dict:
    timestamp = now_iso()
    payload_json = canonical_json(payload)
    nonce, block_hash = await asyncio.to_thread(
        mine_block, index, timestamp, block_type, payload_json, previous_hash
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ledger (block_index, timestamp, block_type, payload, previous_hash, nonce, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (index, timestamp, block_type, payload_json, previous_hash, nonce, block_hash),
        )
        await db.commit()
    logger.info("Ledger block #%d (%s) mined: %s…", index, block_type, block_hash[:16])
    return {
        "block_index": index, "timestamp": timestamp, "block_type": block_type,
        "payload": payload_json, "previous_hash": previous_hash, "nonce": nonce, "hash": block_hash,
    }


async def ensure_genesis() -> None:
    async with _ledger_lock:
        if await _last_block() is None:
            await _write_block(0, "GENESIS", {"network": "TravelBuddy-SafetyChain"}, GENESIS_PREV_HASH)


async def verify_chain() -> dict:
    """Check every block's hash, proof-of-work and link. Returns a summary dict."""
    return verify_blocks(await _fetchall("SELECT * FROM ledger ORDER BY block_index ASC"))


def verify_blocks(blocks: list[dict]) -> dict:
    """Pure verification over blocks sorted by index (shared by the bot and the web portal)."""
    prev_hash = GENESIS_PREV_HASH
    for expected_index, b in enumerate(blocks):
        recomputed = compute_block_hash(
            b["block_index"], b["timestamp"], b["block_type"], b["payload"], b["previous_hash"], b["nonce"]
        )
        if (
            b["block_index"] != expected_index
            or b["previous_hash"] != prev_hash
            or recomputed != b["hash"]
            or not b["hash"].startswith("0" * DIFFICULTY)
        ):
            return {"valid": False, "length": len(blocks), "broken_at": b["block_index"]}
        prev_hash = b["hash"]
    return {"valid": True, "length": len(blocks), "broken_at": None,
            "head": blocks[-1]["hash"] if blocks else None}


async def get_block_by_hash(block_hash: str) -> dict | None:
    return await _fetchone("SELECT * FROM ledger WHERE hash = ?", (block_hash,))


async def get_recent_blocks(limit: int = 5) -> list[dict]:
    return await _fetchall("SELECT * FROM ledger ORDER BY block_index DESC LIMIT ?", (limit,))


# ─────────────────────────────────────────────
#  Digital Tourist ID
# ─────────────────────────────────────────────

def compute_identity_hash(record: dict) -> str:
    fields = {k: record[k] for k in (
        "tourist_id", "telegram_id", "full_name", "nationality", "doc_type",
        "doc_hash", "valid_from", "valid_until",
    )}
    return sha256(canonical_json(fields))


def mask_doc(doc_type: str, last4: str) -> str:
    return f"{doc_type} ••••{last4}"


async def get_active_tourist_id(telegram_id: int) -> dict | None:
    return await _fetchone(
        "SELECT * FROM tourist_ids WHERE telegram_id = ? AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 1",
        (telegram_id,),
    )


async def get_tourist_id(tourist_id: str) -> dict | None:
    return await _fetchone("SELECT * FROM tourist_ids WHERE tourist_id = ?", (tourist_id.upper(),))


async def issue_tourist_id(
    telegram_id: int, full_name: str, nationality: str, doc_type: str, doc_number: str,
    emergency_name: str, emergency_phone: str, valid_days: int,
) -> dict:
    """Create a digital tourist ID and seal its identity hash on the ledger."""
    doc_number = "".join(doc_number.split()).upper()
    salt = secrets.token_hex(8)
    valid_from_dt = datetime.now(timezone.utc)
    record = {
        "tourist_id": "TID-" + secrets.token_hex(5).upper(),
        "telegram_id": telegram_id,
        "full_name": full_name.strip(),
        "nationality": nationality.strip(),
        "doc_type": doc_type,
        "doc_hash": sha256(f"{salt}:{doc_number}"),
        "valid_from": valid_from_dt.isoformat(),
        "valid_until": (valid_from_dt + timedelta(days=valid_days)).isoformat(),
    }
    record["identity_hash"] = compute_identity_hash(record)

    block = await append_block("ID_ISSUED", {
        "tourist_id": record["tourist_id"],
        "identity_hash": record["identity_hash"],
        "valid_until": record["valid_until"],
    })

    await _execute(
        """INSERT INTO tourist_ids
           (tourist_id, telegram_id, full_name, nationality, doc_type, doc_hash, doc_salt, doc_last4,
            emergency_name, emergency_phone, valid_from, valid_until, identity_hash, block_hash,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
        (record["tourist_id"], telegram_id, record["full_name"], record["nationality"], doc_type,
         record["doc_hash"], salt, doc_number[-4:], emergency_name, emergency_phone,
         record["valid_from"], record["valid_until"], record["identity_hash"], block["hash"], now_iso()),
    )
    record.update({"block_hash": block["hash"], "block_index": block["block_index"],
                   "doc_last4": doc_number[-4:], "emergency_name": emergency_name,
                   "emergency_phone": emergency_phone, "status": "active"})
    return record


async def revoke_tourist_id(tourist_id: str, reason: str = "user_request") -> dict:
    block = await append_block("ID_REVOKED", {"tourist_id": tourist_id, "reason": reason})
    await _execute("UPDATE tourist_ids SET status = 'revoked' WHERE tourist_id = ?", (tourist_id,))
    return block


async def verify_tourist_id(tourist_id: str) -> dict:
    """
    Full verification of a tourist ID:
      - record exists, not revoked, not expired
      - DB record matches the identity hash sealed on-chain
      - the sealing block exists and the whole chain is intact
    """
    result = {"tourist_id": tourist_id.upper(), "found": False, "valid": False, "reasons": []}
    record = await get_tourist_id(tourist_id)
    if not record:
        result["reasons"].append("No such Tourist ID on record.")
        return result
    result["found"] = True
    result["record"] = record

    chain = await verify_chain()
    result["chain"] = chain
    if not chain["valid"]:
        result["reasons"].append(f"Ledger integrity broken at block #{chain['broken_at']}.")

    block = await get_block_by_hash(record["block_hash"])
    if not block:
        result["reasons"].append("Sealing block missing from ledger.")
    else:
        result["block_index"] = block["block_index"]
        sealed = json.loads(block["payload"])
        if sealed.get("identity_hash") != compute_identity_hash(record):
            result["reasons"].append("Record has been altered since it was issued (hash mismatch).")
        if sealed.get("tourist_id") != record["tourist_id"]:
            result["reasons"].append("Ledger block does not belong to this ID.")

    if record["status"] != "active":
        result["reasons"].append("ID has been revoked.")
    if datetime.fromisoformat(record["valid_until"]) < datetime.now(timezone.utc):
        result["reasons"].append("ID validity period has expired.")

    result["valid"] = not result["reasons"]
    return result
