"""
incidents.py — Incident creation, responder notifications, check-ins & background monitor.

Responders:
  - Guardians: emergency contacts who linked themselves via a /addguardian invite link.
  - Authorities: Telegram user IDs listed in ADMIN_IDS (.env), e.g. tourism police control room.

Background monitor (runs every MONITOR_INTERVAL_S):
  - Signal lost: active trip but no location for SAFETY_INACTIVITY_MIN → check-in prompt.
  - Safety timer: tourist-set "check on me in N minutes" timer expired → check-in prompt.
  - Unanswered check-in after SAFETY_ESCALATE_MIN → automatic incident + responders alerted.
"""

import asyncio
import hashlib
import logging
import os
import time
from html import escape

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from travel_buddy.config import env_int
from travel_buddy.safety.ledger import append_block, get_active_tourist_id
from travel_buddy.core.database import get_active_or_planning_trip, get_user
from travel_buddy.safety.geofence import maps_link
from travel_buddy.safety.risk_intel import get_weather_risk, invalidate_hotspots
from travel_buddy.safety.store import (
    create_incident, get_all_safety_states, get_incident, get_linked_guardian_ids,
    get_users_seen_near, mark_weather_checked, set_incident_block_hash, update_safety_state,
)
from travel_buddy.core.tracker import haversine_distance_meters

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_S = 60
INACTIVITY_S = env_int("SAFETY_INACTIVITY_MIN", 30) * 60
ESCALATE_S = env_int("SAFETY_ESCALATE_MIN", 15) * 60

SEVERITY_EMOJI = {"low": "🔵", "medium": "🟡", "high": "🟠", "critical": "🔴"}
TYPE_LABEL = {
    "sos": "🆘 SOS panic alert", "medical": "🩺 Medical", "theft": "👜 Theft / loss",
    "harassment": "⚠️ Harassment", "accident": "🚗 Accident", "lost": "🧭 Lost / missing",
    "scam": "💸 Scam / fraud", "natural_hazard": "🌪️ Natural hazard", "other": "📝 Other",
    "geofence_breach": "🚧 Restricted-zone entry", "no_response": "📵 No response to check-in",
}
CHECKIN_REASON_TEXT = {
    "inactivity": "We haven't received your location for a while during your active trip.",
    "timer": "Your safety timer has run out.",
    "ABRUPT_HALT": "Your vehicle seems to have stopped suddenly.",
    "PROLONGED_STOP": "You've been stationary for a long time mid-journey.",
    "ROUTE_DEVIATION": "You seem to be moving away from your destination.",
}


def admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


def is_admin(user_id: int) -> bool:
    return user_id in admin_ids()


def checkin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I'm safe", callback_data="ci:safe"),
        InlineKeyboardButton("🆘 I need help", callback_data="ci:help"),
    ]])


def responder_keyboard(incident_id: int, lat: float | None, lng: float | None) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("👀 Acknowledge", callback_data=f"inc:ack:{incident_id}"),
        InlineKeyboardButton("✅ Resolve", callback_data=f"inc:res:{incident_id}"),
    ]]
    if lat is not None and lng is not None:
        rows.append([InlineKeyboardButton("🗺️ Open in Maps", url=maps_link(lat, lng))])
    return InlineKeyboardMarkup(rows)


async def tourist_label(user_id: int) -> tuple[str, str | None, dict | None]:
    """Return (display name, tourist ID or None, ID record or None)."""
    tid = await get_active_tourist_id(user_id)
    if tid:
        return tid["full_name"], tid["tourist_id"], tid
    user = await get_user(user_id)
    return (user or {}).get("username") or f"User {user_id}", None, None


async def _safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, **kwargs)
        return True
    except TelegramError as exc:
        logger.warning("Could not message %s: %s", chat_id, exc)
        return False


async def notify_responders(bot: Bot, tourist_user_id: int, text: str,
                            lat: float | None = None, lng: float | None = None,
                            incident_id: int | None = None,
                            guardians: bool = True, admins: bool = True) -> int:
    """Send an alert (and location pin) to guardians and/or authorities. Returns delivered count."""
    recipients: set[int] = set()
    if guardians:
        recipients.update(await get_linked_guardian_ids(tourist_user_id))
    if admins:
        recipients.update(admin_ids())
    recipients.discard(tourist_user_id)

    markup = responder_keyboard(incident_id, lat, lng) if incident_id else None
    delivered = 0
    for chat_id in recipients:
        if await _safe_send(bot, chat_id, text, reply_markup=markup):
            delivered += 1
            if lat is not None and lng is not None:
                try:
                    await bot.send_location(chat_id, latitude=lat, longitude=lng)
                except TelegramError:
                    pass
    return delivered


async def raise_incident(
    bot: Bot, user_id: int, incident_type: str, severity: str, description: str,
    lat: float | None, lng: float | None, source: str, ai_advice: str | None = None,
    notify_guardians: bool | None = None, notify_admins: bool | None = None,
) -> dict:
    """Create an incident, seal it on the ledger and alert responders. Returns incident + delivery info."""
    incident_id = await create_incident(user_id, incident_type, severity, description, lat, lng, source, ai_advice)
    name, tourist_id, id_record = await tourist_label(user_id)

    evidence = f"{incident_id}|{user_id}|{incident_type}|{description}|{lat}|{lng}"
    block = await append_block("SOS" if incident_type == "sos" else "INCIDENT", {
        "incident_id": incident_id,
        "tourist_ref": tourist_id or "anon-" + hashlib.sha256(str(user_id).encode()).hexdigest()[:12],
        "type": incident_type,
        "severity": severity,
        "source": source,
        "evidence_hash": hashlib.sha256(evidence.encode()).hexdigest(),
    })
    await set_incident_block_hash(incident_id, block["hash"])

    if notify_guardians is None:
        notify_guardians = severity != "low"
    if notify_admins is None:
        notify_admins = severity in ("high", "critical")

    loc_line = (f"📍 <a href='{maps_link(lat, lng)}'>{lat:.5f}, {lng:.5f}</a>"
                if lat is not None and lng is not None else "📍 Location: <i>not yet shared</i>")
    contact_line = ""
    if id_record and id_record.get("emergency_phone"):
        contact_line = (f"\n☎️ Emergency contact on ID: {escape(id_record.get('emergency_name') or '')} "
                        f"<code>{escape(id_record['emergency_phone'])}</code>")
    alert = (
        f"{SEVERITY_EMOJI.get(severity, '⚪')} <b>{TYPE_LABEL.get(incident_type, incident_type)} — "
        f"{severity.upper()}</b>\n\n"
        f"👤 <b>{escape(name)}</b>" + (f" (<code>{tourist_id}</code>)" if tourist_id else "") + "\n"
        f"{loc_line}\n"
        f"📝 {escape(description[:500])}\n"
        f"🆔 Incident <b>#{incident_id}</b> · ⛓️ block <code>{block['hash'][:16]}…</code>"
        f"{contact_line}"
    )
    delivered = 0
    if notify_guardians or notify_admins:
        delivered = await notify_responders(
            bot, user_id, alert, lat, lng, incident_id,
            guardians=notify_guardians, admins=notify_admins,
        )
    logger.info("Incident #%s (%s/%s) raised for %s; %d responders alerted.",
                incident_id, incident_type, severity, user_id, delivered)
    invalidate_hotspots()  # the learned risk map changes with every new incident
    community = 0
    if lat is not None and incident_type not in COMMUNITY_EXCLUDED and severity != "low":
        community = await broadcast_community_alert(bot, user_id, incident_type, lat, lng)
    incident = await get_incident(incident_id)
    incident["delivered"] = delivered
    incident["community_alerted"] = community
    incident["block_index"] = block["block_index"]
    return incident


# ─────────────────────────────────────────────
#  Community safety network
# ─────────────────────────────────────────────

COMMUNITY_RADIUS_M = 2000.0
COMMUNITY_RECENT_S = 2 * 3600
COMMUNITY_EXCLUDED = {"sos", "no_response", "geofence_breach"}
COMMUNITY_TIPS = {
    "theft": "Keep bags zipped and in front of you; avoid showing phones/cash.",
    "scam": "Use only registered taxis/guides and agree prices in advance.",
    "harassment": "Stay in busy, well-lit places and move in groups if you can.",
    "accident": "Take extra care on the roads around there.",
    "natural_hazard": "Avoid the area and follow local authority instructions.",
    "medical": "Emergency services may be busy nearby.",
    "lost": "Keep your phone charged and share live location with a guardian.",
}


async def broadcast_community_alert(bot: Bot, reporter_id: int, incident_type: str,
                                    lat: float, lng: float) -> int:
    """Anonymously warn other travellers recently seen nearby. Never shares who or exactly where."""
    sent = 0
    for s in await get_users_seen_near(time.time() - COMMUNITY_RECENT_S):
        uid = s["telegram_id"]
        if uid == reporter_id or not s.get("community_alerts", 1):
            continue
        d = haversine_distance_meters(lat, lng, s["last_lat"], s["last_lng"])
        if d > COMMUNITY_RADIUS_M:
            continue
        approx = int(round(d / 100.0) * 100) or 100
        text = (f"📢 <b>Community safety alert</b>\n\nA <b>{TYPE_LABEL.get(incident_type, incident_type)}</b> "
                f"incident was just reported about <b>{approx} m</b> from your last location.\n"
                f"💡 {COMMUNITY_TIPS.get(incident_type, 'Stay alert.')}\n\n<i>/community off to mute these.</i>")
        if await _safe_send(bot, uid, text):
            sent += 1
    return sent


# ─────────────────────────────────────────────
#  Weather watch
# ─────────────────────────────────────────────

WEATHER_CHECK_S = 30 * 60
WEATHER_ACTIVE_S = 2 * 3600
weather_provider = get_weather_risk  # swappable in tests


def _needs_weather_check(s: dict, now: float) -> bool:
    return bool(s["last_seen_ts"]) and now - s["last_seen_ts"] <= WEATHER_ACTIVE_S and not (
        s.get("weather_checked_ts") and now - s["weather_checked_ts"] < WEATHER_CHECK_S)


async def check_weather_batch(bot: Bot, states: list[dict], now: float) -> None:
    """Weather watch for all recently-active users: one batched write, one lookup per ~1 km area."""
    due = [s for s in states if _needs_weather_check(s, now)]
    if not due:
        return
    await mark_weather_checked([s["telegram_id"] for s in due], now)
    by_area: dict[tuple, dict | None] = {}
    for s in due:
        area = (round(s["last_lat"], 2), round(s["last_lng"], 2))
        if area not in by_area:
            try:
                by_area[area] = await weather_provider(s["last_lat"], s["last_lng"])
            except Exception:
                logger.exception("Weather lookup failed for %s", area)
                by_area[area] = None
        w = by_area[area]
        if not w or w["worst"] != "high":
            continue
        key = time.strftime("%Y-%m-%d", time.gmtime(now)) + ":" + ",".join(sorted(h["key"] for h in w["hazards"]))
        if key == s.get("weather_alert_key"):
            continue
        await update_safety_state(s["telegram_id"], weather_alert_key=key)
        await _safe_send(
            bot, s["telegram_id"],
            "🌩️ <b>Severe weather alert near you</b>\n\n" + "\n".join(f"• {h['text']}" for h in w["hazards"])
            + f"\n\n<i>Now: {w['summary']}. Check routes with /saferoute before heading out.</i>",
        )


async def forward_media(bot: Bot, tourist_user_id: int, kind: str, file_id: str, caption: str) -> int:
    """Forward a photo / voice note (evidence) to the tourist's guardians and the safety desk."""
    recipients = set(await get_linked_guardian_ids(tourist_user_id)) | admin_ids()
    recipients.discard(tourist_user_id)
    sent = 0
    for chat_id in recipients:
        try:
            if kind == "photo":
                await bot.send_photo(chat_id, photo=file_id, caption=caption[:1000], parse_mode="HTML")
            else:
                await bot.send_voice(chat_id, voice=file_id, caption=caption[:1000], parse_mode="HTML")
            sent += 1
        except TelegramError as exc:
            logger.warning("Could not forward %s to %s: %s", kind, chat_id, exc)
    return sent


async def send_checkin(bot: Bot, user_id: int, reason: str, detail: str = "") -> None:
    text = (
        "🛡️ <b>Safety check-in</b>\n\n"
        f"{CHECKIN_REASON_TEXT.get(reason, 'Just checking on you.')}\n"
        + (f"<i>{escape(detail)}</i>\n" if detail else "")
        + f"\nAre you okay? If you don't answer within <b>{int(ESCALATE_S // 60)} min</b>, "
          "your guardians and the safety desk will be alerted."
    )
    await _safe_send(bot, user_id, text, reply_markup=checkin_keyboard())
    await update_safety_state(user_id, checkin_pending_ts=time.time(), checkin_reason=reason)


async def run_monitor_cycle(bot: Bot, now: float | None = None) -> None:
    now = now or time.time()
    states = await get_all_safety_states()
    try:
        await check_weather_batch(bot, states, now)
    except Exception:
        logger.exception("Weather watch failed")
    for s in states:
        uid = s["telegram_id"]
        try:
            if s["checkin_pending_ts"]:
                if now - s["checkin_pending_ts"] >= ESCALATE_S:
                    reason = s["checkin_reason"] or "unknown"
                    await update_safety_state(uid, checkin_pending_ts=None, checkin_reason=None)
                    minutes_silent = (now - (s["last_seen_ts"] or now)) / 60
                    incident = await raise_incident(
                        bot, uid, "no_response", "high",
                        f"No reply to safety check-in ({reason}). Last location update "
                        f"{minutes_silent:.0f} min ago.",
                        s["last_lat"], s["last_lng"], source=f"auto_{reason}",
                        notify_guardians=True, notify_admins=True,
                    )
                    await _safe_send(
                        bot, uid,
                        f"🚨 <b>No response received — responders alerted</b> (incident #{incident['incident_id']}).\n"
                        "Tap below or send /safe once you are okay.",
                        reply_markup=checkin_keyboard(),
                    )
                continue

            if s["timer_due_ts"] and now >= s["timer_due_ts"]:
                await update_safety_state(uid, timer_due_ts=None)
                await send_checkin(bot, uid, "timer")
                continue

            if s["monitoring_enabled"] and not s["inactivity_flagged"] and s["last_seen_ts"]:
                if now - s["last_seen_ts"] >= INACTIVITY_S:
                    trip = await get_active_or_planning_trip(uid)
                    if trip and trip["status"] == "active":
                        await update_safety_state(uid, inactivity_flagged=1)
                        await send_checkin(
                            bot, uid, "inactivity",
                            f"Last update {int((now - s['last_seen_ts']) // 60)} min ago. "
                            "Your live location sharing may have stopped.",
                        )
        except Exception:
            logger.exception("Safety monitor failed for user %s", uid)


async def monitor_loop(bot: Bot) -> None:
    logger.info("Safety monitor started (inactivity=%.0f min, escalate=%.0f min).",
                INACTIVITY_S / 60, ESCALATE_S / 60)
    while True:
        await asyncio.sleep(MONITOR_INTERVAL_S)
        try:
            await run_monitor_cycle(bot)
        except Exception:
            logger.exception("Safety monitor cycle crashed")
