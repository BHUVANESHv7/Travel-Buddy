"""
actions.py — Safety actions shared by the Telegram bot and the web portal.

Every function here is channel-neutral: it takes the Telegram `bot` (used to alert
responders) and, where the user should be told something, a `say(html)` coroutine.
The bot passes `message.reply_html`; the website passes a collector and shows the
messages in the browser. One implementation → identical behaviour on both channels.
"""

import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from html import escape
from typing import Awaitable, Callable

from travel_buddy.core.database import get_active_or_planning_trip
from travel_buddy.safety.geofence import RISK_EMOJI, RISK_ORDER, evaluate_geofence, maps_link, reverse_geocode
from travel_buddy.safety.incidents import (
    TYPE_LABEL, is_admin, notify_responders, raise_incident, send_checkin, tourist_label,
)
from travel_buddy.safety import forecast
from travel_buddy.safety.ledger import append_block, mask_doc
from travel_buddy.safety.monitor import analyze_motion
from travel_buddy.safety.risk_intel import HOTSPOT_WARN_M, get_hotspots, hotspots_near
from travel_buddy.safety.store import (
    acknowledge_incident, create_guardian_invite, get_active_zones, get_evidence, get_location_history,
    get_open_user_incidents, get_safety_state, is_guardian_of, record_location, resolve_incident,
    set_incident_location, update_safety_state,
)

logger = logging.getLogger(__name__)

Say = Callable[[str], Awaitable[object]]

LOCATION_FRESH_S = 15 * 60
HOTSPOT_REWARN_S = 6 * 3600
HOTSPOT_TIPS = {
    "theft": "Keep bags zipped and in front of you; don't flash phones or cash.",
    "scam": "Agree prices first; use registered guides/taxis only.",
    "harassment": "Stay in busy, well-lit areas; move in a group if possible.",
    "accident": "Extra care crossing roads and on vehicles here.",
    "natural_hazard": "Hazard-prone ground — keep to marked, safe paths.",
}


async def _silent(_html: str) -> None:
    return None


# ─────────────────────────────────────────────
#  Permissions
# ─────────────────────────────────────────────

async def can_manage(uid: int, incident: dict) -> bool:
    """Owner, one of the owner's guardians, or the safety desk."""
    return (incident["telegram_id"] == uid or is_admin(uid)
            or await is_guardian_of(uid, incident["telegram_id"]))


def role_of(uid: int, incident: dict) -> str:
    return "tourist" if uid == incident["telegram_id"] else "admin" if is_admin(uid) else "guardian"


# ─────────────────────────────────────────────
#  Incidents
# ─────────────────────────────────────────────

async def acknowledge_as(bot, inc: dict, by_uid: int, responder_name: str) -> None:
    await acknowledge_incident(inc["incident_id"], by_uid)
    if by_uid != inc["telegram_id"]:
        try:
            await bot.send_message(
                inc["telegram_id"],
                f"👀 <b>{escape(responder_name)}</b> has seen your alert (incident #{inc['incident_id']}) "
                "and is responding.", parse_mode="HTML")
        except Exception:
            pass


async def resolve_as(bot, inc: dict, by_uid: int, role: str | None = None) -> bool:
    if inc["status"] == "resolved":
        return False
    await resolve_incident(inc["incident_id"], by_uid)
    role = role or role_of(by_uid, inc)
    await append_block("INCIDENT_RESOLVED", {"incident_id": inc["incident_id"], "by": role})
    if by_uid != inc["telegram_id"]:
        try:
            await bot.send_message(inc["telegram_id"], f"🟢 Incident #{inc['incident_id']} was marked resolved by a {role}.")
        except Exception:
            pass
    return True


async def mark_safe(bot, uid: int) -> dict:
    """'I'm safe': clears check-ins and timer, closes SOS/automatic incidents, tells responders."""
    state = await get_safety_state(uid)
    await update_safety_state(uid, checkin_pending_ts=None, checkin_reason=None, timer_due_ts=None)
    resolved = []
    for inc in await get_open_user_incidents(uid):
        if inc["source"].startswith("auto_") or inc["incident_type"] == "sos":
            await resolve_incident(inc["incident_id"], uid)
            await append_block("INCIDENT_RESOLVED", {"incident_id": inc["incident_id"], "by": "tourist"})
            resolved.append(inc["incident_id"])
    if resolved:
        name, _, _ = await tourist_label(uid)
        await notify_responders(
            bot, uid,
            f"✅ <b>{escape(name)}</b> has confirmed they are <b>safe</b>. "
            f"Incident(s) {', '.join('#' + str(i) for i in resolved)} closed.",
        )
    return {"resolved": resolved, "cleared": bool(state["checkin_pending_ts"] or state["timer_due_ts"])}


def mark_safe_text(result: dict) -> str:
    msg = "✅ <b>Thanks — you're marked safe.</b>"
    if result["cleared"]:
        msg += " Check-in and timer cleared."
    if result["resolved"]:
        msg += f"\nClosed incident(s): {', '.join('#' + str(i) for i in result['resolved'])} and informed responders."
    return msg


async def trigger_sos(bot, uid: int, source: str = "sos", note: str = "") -> dict:
    """Raise a critical SOS with the freshest known location. Returns the incident (+ 'fresh')."""
    state = await get_safety_state(uid)
    lat, lng, seen = state["last_lat"], state["last_lng"], state["last_seen_ts"]
    fresh = seen is not None and time.time() - seen <= LOCATION_FRESH_S
    description = note or "Panic button pressed."
    if lat is not None and not fresh:
        mins = int((time.time() - seen) // 60)
        description += f" Location is last known ({mins} min ago)."
    incident = await raise_incident(bot, uid, "sos", "critical", description, lat, lng,
                                    source=source, notify_guardians=True, notify_admins=True)
    incident["fresh"] = fresh
    return incident


async def set_timer(uid: int, minutes: int) -> float | None:
    due = time.time() + minutes * 60 if minutes else None
    await update_safety_state(uid, timer_due_ts=due)
    return due


async def new_guardian_invite(uid: int, name: str, bot_username: str) -> str:
    code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    await create_guardian_invite(uid, name[:60] or "Guardian", code)
    return f"https://t.me/{bot_username}?start=g_{code}"


# ─────────────────────────────────────────────
#  Location ingestion (geo-fencing, anomalies, hotspots)
# ─────────────────────────────────────────────

async def ingest_location(bot, uid: int, lat: float, lng: float, trip: dict | None = None,
                          alert_already_sent: bool = False, say: Say | None = None,
                          analyze: bool = True) -> None:
    """Process one location update from any channel. User-facing notes go through `say`."""
    say = say or _silent
    now = time.time()
    state = await get_safety_state(uid)

    fields = {"last_lat": lat, "last_lng": lng, "last_seen_ts": now, "inactivity_flagged": 0}
    if state["checkin_pending_ts"] and state["checkin_reason"] == "inactivity":
        fields.update(checkin_pending_ts=None, checkin_reason=None)
        await say("📶 <b>Location signal restored</b> — safety check-in cleared. Stay safe!")
    await update_safety_state(uid, **fields)
    await record_location(uid, lat, lng, now)

    # Attach location to a recent SOS that was raised without one.
    for inc in await get_open_user_incidents(uid):
        if inc["incident_type"] == "sos" and inc["lat"] is None:
            await set_incident_location(inc["incident_id"], lat, lng)
            await notify_responders(bot, uid, f"📍 <b>Location update for SOS #{inc['incident_id']}</b>",
                                    lat, lng, inc["incident_id"])
            await say(f"📍 Location attached to SOS #{inc['incident_id']} and sent to responders.")

    # ── Geo-fencing ──
    zones = await get_active_zones()
    events, new_zone_state = evaluate_geofence(lat, lng, zones, state["zone_state"])
    if new_zone_state != state["zone_state"]:
        await update_safety_state(uid, zone_state=new_zone_state)
    for ev in events:
        z = ev.zone
        emoji = RISK_EMOJI[z["risk_level"]]
        if ev.kind == "APPROACH":
            await say(f"{emoji} <b>Heads-up: {escape(z['name'])}</b> is ~{ev.distance_to_edge_m:.0f} m ahead "
                      f"(<b>{z['risk_level']}</b> zone).\n{escape(z['advisory'] or '')}")
        elif ev.kind == "ENTER":
            await say(
                f"{emoji} <b>You entered: {escape(z['name'])}</b>\n"
                f"Risk level: <b>{z['risk_level'].upper()}</b>\n\n⚠️ {escape(z['advisory'] or 'Stay alert.')}"
                + ("\n\n<i>Your guardians and the safety desk have been notified.</i>"
                   if z["risk_level"] == "restricted" else
                   "\n\n<i>Your guardians have been notified.</i>" if z["risk_level"] == "high" else ""))
            if z["risk_level"] == "restricted":
                await raise_incident(bot, uid, "geofence_breach", "medium",
                                     f"Entered restricted zone '{z['name']}'.", lat, lng,
                                     source="auto_geofence", notify_guardians=True, notify_admins=True)
            elif z["risk_level"] == "high":
                name, _, _ = await tourist_label(uid)
                await notify_responders(bot, uid,
                                        f"🟠 <b>{escape(name)}</b> entered high-risk zone <b>{escape(z['name'])}</b>.",
                                        lat, lng, guardians=True, admins=False)
        elif ev.kind == "EXIT" and RISK_ORDER[z["risk_level"]] >= RISK_ORDER["caution"]:
            await say(f"🟢 You left <b>{escape(z['name'])}</b>. Safer ground — well done.")

    # ── Behavioural anomaly detection (meaningful during a trip) ──
    if analyze and state["monitoring_enabled"]:
        for anomaly in analyze_motion(uid, lat, lng, trip, alert_already_sent, ts=now):
            if (await get_safety_state(uid))["checkin_pending_ts"]:
                continue
            await send_checkin(bot, uid, anomaly.kind, anomaly.message)

    await hotspot_warnings(uid, lat, lng, say)


async def hotspot_warnings(uid: int, lat: float, lng: float, say: Say) -> None:
    """Warn (at most every 6 h per place) when entering or approaching a learned incident hotspot,
    or when the predictive model expects incident risk here to be elevated now or within 3 hours."""
    near = hotspots_near(lat, lng, await get_hotspots(), within_m=HOTSPOT_WARN_M)
    model = await forecast.get_model()
    if not near and not model.events:
        return
    state = await get_safety_state(uid)
    try:
        warned = json.loads(state.get("hotspot_alerts") or "{}")
    except ValueError:
        warned = {}
    now, changed = time.time(), False
    if not near:
        warning = forecast.proactive_warning(model.predict(lat, lng, hours=4))
        key = f"F:{lat:.2f},{lng:.2f}"  # ~1 km cell
        if warning and now - warned.get(key, 0) >= HOTSPOT_REWARN_S:
            warned[key] = now
            await say(warning)
            await update_safety_state(uid, hotspot_alerts=json.dumps(
                {k: v for k, v in warned.items() if now - v < 86400}))
        return
    for h, d in near[:2]:
        key = f"{h['lat']:.3f},{h['lng']:.3f}"
        if now - warned.get(key, 0) < HOTSPOT_REWARN_S:
            continue
        warned[key], changed = now, True
        where = "You're inside" if d <= 0 else f"~{d:.0f} m ahead is"
        await say(f"🔥 <b>Incident hotspot</b> — {where} an area with <b>{h['count']} recent "
                  f"{escape(h['dominant_type'])}</b> reports (learned automatically).\n"
                  f"💡 {HOTSPOT_TIPS.get(h['dominant_type'], 'Stay alert and keep to busy areas.')}")
    if changed:
        warned = {k: v for k, v in warned.items() if now - v < 86400}
        await update_safety_state(uid, hotspot_alerts=json.dumps(warned))


async def current_trip(uid: int) -> dict | None:
    return await get_active_or_planning_trip(uid)


# ─────────────────────────────────────────────
#  E-FIR draft
# ─────────────────────────────────────────────

def _fmt_date(iso: str | None) -> str:
    return iso[:16].replace("T", " ") + " UTC" if iso else "—"


async def build_efir_text(inc: dict) -> str:
    name, tid, rec = await tourist_label(inc["telegram_id"])
    trail = await get_location_history(inc["telegram_id"], 8)
    evidence_rows = await get_evidence(inc["incident_id"])
    place = ""
    if inc["lat"] is not None:
        geo = await reverse_geocode(inc["lat"], inc["lng"])
        place = geo["display_name"] if geo else ""
    coords = f"{inc['lat']:.6f}, {inc['lng']:.6f}" if inc["lat"] is not None else "not provided"
    trail_lines = [
        f"  {datetime.fromtimestamp(p['ts'], timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC  "
        f"{p['lat']:.5f}, {p['lng']:.5f}" for p in trail
    ] or ["  none recorded"]
    return "\n".join([
        "DRAFT FIRST INFORMATION REPORT (E-FIR) — generated by Travel Buddy Safety",
        "=" * 72,
        f"Generated      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Incident No.   : {inc['incident_id']}",
        f"Category       : {TYPE_LABEL.get(inc['incident_type'], inc['incident_type'])}",
        f"Severity       : {inc['severity'].upper()}",
        f"Reported at    : {_fmt_date(inc['created_at'])}",
        f"Status         : {inc['status']}",
        "",
        "COMPLAINANT",
        f"  Name         : {name}",
        f"  Tourist ID   : {tid or 'not registered'}",
        f"  Nationality  : {rec['nationality'] if rec else '-'}",
        f"  ID document  : {mask_doc(rec['doc_type'], rec['doc_last4']) if rec else '-'}",
        f"  Emergency    : {(rec.get('emergency_name') or '') + ' ' + (rec.get('emergency_phone') or '') if rec else '-'}",
        "",
        "PLACE OF OCCURRENCE",
        f"  Coordinates  : {coords}",
        f"  Address      : {place or '-'}",
        f"  Map          : {maps_link(inc['lat'], inc['lng']) if inc['lat'] is not None else '-'}",
        "",
        "DESCRIPTION OF INCIDENT",
        f"  {inc['description'] or '-'}",
        "",
        "RECENT MOVEMENT TRAIL (latest first)",
        *trail_lines,
        "",
        "INTEGRITY PROOF",
        f"  Ledger block : {inc['block_hash'] or '-'}",
        *[f"  Evidence #{e['evidence_id']} ({e['kind']}) SHA-256: {e['sha256']}"
          + (f"\n      {'transcript' if e['kind'] == 'voice' else 'note'}: {e['transcript'][:200]}"
             if e.get("transcript") else "")
          for e in evidence_rows],
        "  Verify       : /ledger and /verifyid in the Travel Buddy bot, or the web portal",
        "",
        "Declaration: The above information is true to the best of my knowledge.",
        "Signature of complainant: ______________________   Date: ____________",
        "",
        "NOTE: This is a draft to help you file a complaint. Submit it at the nearest police station",
        "or through the state police online portal.",
    ])


# ─────────────────────────────────────────────
#  Photo evidence (shared by Telegram photos and web uploads)
# ─────────────────────────────────────────────

EVIDENCE_WINDOW_S = 24 * 3600


async def evidence_target(uid: int) -> dict | None:
    """The user's most recent open incident from the last 24 h (photos attach to it by default)."""
    for inc in await get_open_user_incidents(uid):
        try:
            age = time.time() - datetime.fromisoformat(inc["created_at"]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            age = 0
        if age <= EVIDENCE_WINDOW_S and inc["incident_type"] != "no_response":
            return inc
    return None


async def add_photo_evidence(bot, uid: int, data: bytes, caption: str = "", file_id: str | None = None,
                             incident: dict | None = None) -> dict:
    """Seal a photo's SHA-256 on the ledger, attach it to an incident and forward it to responders.

    incident: attach to this one; otherwise the latest open incident (24 h); otherwise a new
    incident is opened from the caption (AI triage). file_id: the photo already on Telegram;
    if None (web upload) the photo is first sent to the user's own chat to obtain one.
    """
    from travel_buddy.ai.triage import classify_incident
    from travel_buddy.safety.incidents import SEVERITY_EMOJI, forward_media
    from travel_buddy.safety.store import add_evidence

    caption = (caption or "").strip()[:500]
    target = incident or await evidence_target(uid)
    created = False
    if target is None:
        if len(caption) < 3:
            raise ValueError("Add a short caption describing what happened, so a new incident can be opened.")
        state = await get_safety_state(uid)
        triage = await classify_incident(caption)
        target = await raise_incident(bot, uid, triage["category"], triage["severity"], caption,
                                      state["last_lat"], state["last_lng"], source="photo", ai_advice=triage["advice"])
        created = True

    label = f"{SEVERITY_EMOJI.get(target['severity'], '')} {TYPE_LABEL.get(target['incident_type'], target['incident_type'])}"
    digest = hashlib.sha256(data).hexdigest()
    if file_id is None:
        try:
            msg = await bot.send_photo(uid, photo=data, caption=f"📸 Evidence saved from the web portal · incident "
                                                               f"#{target['incident_id']} ({label})")
            file_id = msg.photo[-1].file_id if msg and getattr(msg, "photo", None) else None
        except Exception as exc:
            logger.warning("Could not upload web evidence to Telegram: %s", exc)
        file_id = file_id or f"web:{digest}"

    block = await append_block("EVIDENCE", {"incident_id": target["incident_id"], "kind": "photo", "sha256": digest})
    await add_evidence(target["incident_id"], uid, "photo", file_id, digest, caption or None, block["hash"])
    forwarded = 0
    if (target["severity"] != "low" or target["incident_type"] == "sos") and not file_id.startswith("web:"):
        forwarded = await forward_media(bot, uid, "photo", file_id,
                                        f"📸 Evidence for incident #{target['incident_id']} ({label})"
                                        + (f"\n“{escape(caption[:300])}”" if caption else ""))
    return {"incident": target, "created": created, "sha256": digest,
            "block_index": block["block_index"], "forwarded": forwarded}
