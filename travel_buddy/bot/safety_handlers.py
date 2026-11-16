"""
safety_handlers.py — Telegram handlers for the Smart Tourist Safety system.

Digital ID (blockchain):   /registerid /myid /verifyid /revokeid /ledger
Guardians:                 /addguardian /guardians /removeguardian  (+ /start g_<code> deep link)
Monitoring & geo-fencing:  /safety /zones /nearby /safetytips /timer /safe /monitoring
Incident response:         /sos /report /incidents /resolve /efir
Authorities (ADMIN_IDS):   /dashboard /addzone /removezone /whereis
"""

import io
import json
import logging
import re
import time
from html import escape

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, ConversationHandler

from travel_buddy.ai.triage import classify_incident, generate_safety_briefing
from travel_buddy.safety.ledger import (
    get_active_tourist_id, get_recent_blocks, issue_tourist_id, mask_doc,
    revoke_tourist_id, verify_chain, verify_tourist_id, get_tourist_id,
)
from travel_buddy.core.database import get_active_or_planning_trip, upsert_user
from travel_buddy.safety.geofence import (
    RISK_EMOJI, emergency_numbers_for, find_nearby_services,
    highest_risk, maps_link, reverse_geocode, zones_near, SERVICE_LABELS, RISK_ORDER,
)
from travel_buddy.safety.incidents import TYPE_LABEL, SEVERITY_EMOJI, is_admin, raise_incident, tourist_label
from travel_buddy.bot.keyboards import BTN_MYID, BTN_NEARBY, BTN_SAFETY, BTN_SOS, MAIN_KEYBOARD
from travel_buddy.safety.store import (
    _fetchall, _fetchone, add_zone, deactivate_zone, get_active_zones, get_guardians, get_incident,
    get_open_incidents, get_open_user_incidents, get_safety_state, get_tourists_guarded_by,
    get_user_incidents, is_guardian_of, link_guardian, remove_guardian, update_safety_state,
)
from travel_buddy.safety.actions import (
    acknowledge_as, build_efir_text, can_manage, ingest_location, mark_safe, mark_safe_text,
    new_guardian_invite, resolve_as, trigger_sos,
)
from travel_buddy.safety.monitor import (
    approx_local_hour, compute_safety_score, recent_anomaly_count, reset_motion,
)

logger = logging.getLogger(__name__)

REG_NAME, REG_NATIONALITY, REG_DOC_TYPE, REG_DOC_NUMBER, REG_EMERGENCY, REG_DAYS = range(20, 26)
REP_TYPE, REP_DESC, REP_LOC = range(30, 33)

LOCATION_FRESH_S = 15 * 60

REPORT_TYPES = {
    "🩺 Medical": "medical", "👜 Theft / Loss": "theft", "⚠️ Harassment": "harassment",
    "🚗 Accident": "accident", "🧭 Lost / Missing": "lost", "💸 Scam / Fraud": "scam",
    "🌪️ Natural hazard": "natural_hazard", "📝 Other": "other",
}
DOC_TYPES = [["Passport", "Aadhaar"], ["Driving Licence", "Other ID"]]


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    mins = int((time.time() - ts) // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    return f"{mins // 60} h {mins % 60} min ago"


def _fmt_date(iso: str | None) -> str:
    return iso[:16].replace("T", " ") + " UTC" if iso else "—"


async def _last_known_location(user_id: int) -> tuple[float | None, float | None, float | None]:
    s = await get_safety_state(user_id)
    return s["last_lat"], s["last_lng"], s["last_seen_ts"]


# ═════════════════════════════════════════════
#  Digital Tourist ID (blockchain)
# ═════════════════════════════════════════════

async def registerid_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    existing = await get_active_tourist_id(user.id)
    if existing:
        await update.effective_message.reply_html(
            f"🪪 You already have an active Digital Tourist ID: <code>{existing['tourist_id']}</code>.\n"
            "View it with /myid, or /revokeid first to issue a new one."
        )
        return ConversationHandler.END

    context.user_data["reg"] = {}
    await update.effective_message.reply_html(
        "🪪 <b>Digital Tourist ID — Registration</b>\n\n"
        "Your ID is sealed on a tamper-proof blockchain ledger so police, hotels and guides can verify it "
        "instantly. Your document number is <b>never stored</b> — only a salted hash.\n\n"
        "<b>Step 1/6:</b> Your full name (as on your ID document)?\n\n<i>/cancel to abort</i>"
    )
    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 80:
        await update.message.reply_text("Please enter a valid name (2–80 characters).")
        return REG_NAME
    context.user_data["reg"]["full_name"] = name
    await update.message.reply_html("<b>Step 2/6:</b> Your nationality? (e.g. Indian, German)")
    return REG_NATIONALITY


async def reg_nationality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["reg"]["nationality"] = update.message.text.strip()[:40]
    await update.message.reply_html(
        "<b>Step 3/6:</b> Which ID document are you registering?",
        reply_markup=ReplyKeyboardMarkup(DOC_TYPES, one_time_keyboard=True, resize_keyboard=True),
    )
    return REG_DOC_TYPE


async def reg_doc_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["reg"]["doc_type"] = update.message.text.strip()[:30]
    await update.message.reply_html(
        "<b>Step 4/6:</b> Enter the document number.\n"
        "🔒 <i>It is hashed immediately, never saved, and I'll delete your message from this chat.</i>",
        reply_markup=MAIN_KEYBOARD,
    )
    return REG_DOC_NUMBER


async def reg_doc_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = "".join(update.message.text.split())
    try:
        await update.message.delete()
    except Exception:
        pass
    if not re.fullmatch(r"[A-Za-z0-9\-]{4,24}", doc):
        await update.effective_chat.send_message("That doesn't look like a valid document number (4–24 letters/digits). Try again:")
        return REG_DOC_NUMBER
    context.user_data["reg"]["doc_number"] = doc
    await update.effective_chat.send_message(
        "✅ Document number received and removed from chat.\n\n"
        "<b>Step 5/6:</b> Emergency contact — send as <code>Name, phone</code>\n"
        "e.g. <code>Priya Sharma, +91 98765 43210</code>",
        parse_mode="HTML",
    )
    return REG_EMERGENCY


async def reg_emergency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if "," not in text:
        await update.message.reply_html("Please use the format <code>Name, phone</code>.")
        return REG_EMERGENCY
    name, phone = [p.strip() for p in text.split(",", 1)]
    if not re.fullmatch(r"\+?[\d\s\-()]{6,20}", phone):
        await update.message.reply_html("That phone number looks invalid. Format: <code>Name, +91 98765 43210</code>")
        return REG_EMERGENCY
    context.user_data["reg"]["emergency_name"] = name[:60]
    context.user_data["reg"]["emergency_phone"] = phone
    await update.message.reply_html(
        "<b>Step 6/6:</b> How many days will you be travelling? (ID validity)",
        reply_markup=ReplyKeyboardMarkup([["3", "7", "15"], ["30", "90"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return REG_DAYS


async def reg_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int(update.message.text.strip())
        assert 1 <= days <= 365
    except (ValueError, AssertionError):
        await update.message.reply_text("Please send a number of days between 1 and 365.")
        return REG_DAYS

    reg = context.user_data.pop("reg", {})
    # No reply keyboard on a progress message: Telegram refuses to edit/replace those.
    status = await update.message.reply_html("⛏️ <i>Mining your ID block on the safety ledger…</i>")
    record = await issue_tourist_id(
        telegram_id=update.effective_user.id,
        full_name=reg["full_name"], nationality=reg["nationality"], doc_type=reg["doc_type"],
        doc_number=reg["doc_number"], emergency_name=reg["emergency_name"],
        emergency_phone=reg["emergency_phone"], valid_days=days,
    )
    await _replace_status(
        update, status,
        "✅ <b>Digital Tourist ID issued!</b>\n\n" + _id_card(record) +
        "\n\n💡 Anyone can verify it with <code>/verifyid " + record["tourist_id"] + "</code>.\n"
        "Next: add an emergency contact who gets your alerts → /addguardian",
    )
    from travel_buddy.bot.intel_handlers import send_id_qr
    await send_id_qr(update, record)
    return ConversationHandler.END


def _id_card(r: dict) -> str:
    return (
        "┏━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "   🪪 <b>DIGITAL TOURIST ID</b>\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━┛\n"
        f"<b>ID:</b> <code>{r['tourist_id']}</code>\n"
        f"<b>Name:</b> {escape(r['full_name'])}\n"
        f"<b>Nationality:</b> {escape(r['nationality'])}\n"
        f"<b>Document:</b> {escape(mask_doc(r['doc_type'], r['doc_last4']))}\n"
        f"<b>Valid:</b> {_fmt_date(r['valid_from'])[:10]} → {_fmt_date(r['valid_until'])[:10]}\n"
        f"<b>Emergency contact:</b> {escape(r.get('emergency_name') or '—')} {escape(r.get('emergency_phone') or '')}\n"
        f"<b>Status:</b> {'🟢 Active' if r.get('status', 'active') == 'active' else '🔴 Revoked'}\n"
        f"⛓️ <b>Block hash:</b> <code>{r['block_hash'][:32]}…</code>"
    )


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    context.user_data.pop("report", None)
    await update.effective_message.reply_html("🚫 Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def myid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    record = await get_active_tourist_id(update.effective_user.id)
    if not record:
        await update.effective_message.reply_html(
            "🪪 You don't have a Digital Tourist ID yet. Create one with /registerid (takes 1 minute)."
        )
        return
    check = await verify_tourist_id(record["tourist_id"])
    verdict = "✅ Verified on ledger" if check["valid"] else "⚠️ " + "; ".join(check["reasons"])
    await update.effective_message.reply_html(_id_card(record) + f"\n\n<b>Verification:</b> {verdict}")
    from travel_buddy.bot.intel_handlers import send_id_qr
    await send_id_qr(update, record)


async def verifyid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_html("Usage: <code>/verifyid TID-XXXXXXXXXX</code>")
        return
    result = await verify_tourist_id(context.args[0].strip())
    if not result["found"]:
        await update.effective_message.reply_html(f"❌ <b>{escape(result['tourist_id'])}</b> — not found on record.")
        return
    r = result["record"]
    head = "✅ <b>VALID Tourist ID</b>" if result["valid"] else "❌ <b>INVALID Tourist ID</b>"
    lines = [
        head, "",
        f"<b>ID:</b> <code>{r['tourist_id']}</code>",
        f"<b>Name:</b> {escape(r['full_name'])}",
        f"<b>Nationality:</b> {escape(r['nationality'])}",
        f"<b>Document:</b> {escape(mask_doc(r['doc_type'], r['doc_last4']))}",
        f"<b>Valid until:</b> {_fmt_date(r['valid_until'])}",
        f"<b>Sealed in block:</b> #{result.get('block_index', '?')}",
        f"<b>Ledger integrity:</b> {'intact' if result['chain']['valid'] else 'BROKEN'} "
        f"({result['chain']['length']} blocks)",
    ]
    if result["reasons"]:
        lines += ["", "<b>Problems:</b>"] + [f"• {escape(x)}" for x in result["reasons"]]
    await update.effective_message.reply_html("\n".join(lines))


async def revokeid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    record = await get_active_tourist_id(update.effective_user.id)
    if not record:
        await update.effective_message.reply_text("You have no active Tourist ID to revoke.")
        return
    block = await revoke_tourist_id(record["tourist_id"])
    await update.effective_message.reply_html(
        f"🔴 Tourist ID <code>{record['tourist_id']}</code> revoked (block #{block['block_index']}).\n"
        "Issue a new one anytime with /registerid."
    )


async def ledger_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chain = await verify_chain()
    blocks = await get_recent_blocks(6)
    lines = [
        "⛓️ <b>Safety Ledger</b>",
        f"<b>Blocks:</b> {chain['length']} · <b>Integrity:</b> "
        + ("✅ intact" if chain["valid"] else f"❌ broken at block #{chain['broken_at']}"),
        "<i>Blocks store only hashes & IDs — no personal data.</i>", "",
    ]
    for b in blocks:
        lines.append(f"#{b['block_index']} <b>{b['block_type']}</b> · {_fmt_date(b['timestamp'])}\n"
                     f"   <code>{b['hash'][:24]}…</code> ← <code>{b['previous_hash'][:10]}…</code>")
    await update.effective_message.reply_html("\n".join(lines))


# ═════════════════════════════════════════════
#  Guardians (emergency contacts)
# ═════════════════════════════════════════════

async def addguardian_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    name = " ".join(context.args).strip()[:60] if context.args else "Guardian"
    link = await new_guardian_invite(user.id, name, context.bot.username)
    await update.effective_message.reply_html(
        "👨‍👩‍👧 <b>Guardian invite created</b>\n\n"
        f"Send this link to <b>{escape(name)}</b> (family member, friend, tour operator):\n{link}\n\n"
        "When they open it and press <b>Start</b>, they'll receive your SOS alerts, live-location pins, "
        "restricted-zone warnings and missed check-ins.\n<i>The link works once.</i>"
    )


async def handle_guardian_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    guardian = update.effective_user
    row = await link_guardian(code, guardian.id)
    if not row:
        await update.effective_message.reply_html("⚠️ This guardian link is invalid or was already used.")
        return
    name, tid, _ = await tourist_label(row["tourist_telegram_id"])
    await update.effective_message.reply_html(
        f"🛡️ You are now a <b>guardian</b> for <b>{escape(name)}</b>"
        + (f" (<code>{tid}</code>)" if tid else "") + ".\n\n"
        "You'll be alerted here if they press SOS, enter a restricted zone or miss a safety check-in.\n"
        "Use /whereis to see their last known location."
    )
    try:
        await context.bot.send_message(
            row["tourist_telegram_id"],
            f"✅ <b>{escape(guardian.full_name)}</b> accepted your guardian invite and will receive your safety alerts.",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def guardians_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await get_guardians(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_html("You have no guardians yet. Add one with <code>/addguardian Name</code>.")
        return
    lines = ["👨‍👩‍👧 <b>Your guardians</b>", ""]
    for r in rows:
        state = "✅ linked" if r["guardian_telegram_id"] else "⏳ invite not accepted"
        lines.append(f"<code>{r['guardian_row_id']}</code> · {escape(r['guardian_name'] or 'Guardian')} — {state}")
    lines.append("\nRemove one with <code>/removeguardian ID</code>.")
    await update.effective_message.reply_html("\n".join(lines))


async def removeguardian_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_html("Usage: <code>/removeguardian ID</code> (see /guardians)")
        return
    ok = await remove_guardian(update.effective_user.id, int(context.args[0]))
    await update.effective_message.reply_text("Guardian removed." if ok else "No guardian with that ID.")


# ═════════════════════════════════════════════
#  Location hook: geo-fencing + anomaly detection
# ═════════════════════════════════════════════

async def safety_location_hook(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               lat: float, lng: float, trip: dict | None,
                               alert_already_sent: bool = False) -> None:
    """Called for every location ping (single pin or live stream). Shared logic lives in actions."""
    await ingest_location(context.bot, update.effective_user.id, lat, lng, trip, alert_already_sent,
                          say=update.effective_message.reply_html)


async def location_snapshot_reply(update: Update, lat: float, lng: float) -> None:
    """Reply to a one-off location pin when the user has no trip."""
    uid = update.effective_user.id
    state = await get_safety_state(uid)
    zones = await get_active_zones()
    inside = [z for z in zones if z["zone_id"] in state["zone_state"]["inside"]]
    score = await _score_for(uid, state, inside)
    zone_line = ", ".join(f"{RISK_EMOJI[z['risk_level']]} {escape(z['name'])}" for z in inside) or "🟢 No risk zones here"
    await update.effective_message.reply_html(
        "📍 <b>Location received</b>\n\n"
        f"<b>Zone:</b> {zone_line}\n"
        f"<b>Safety score:</b> {score['emoji']} {score['score']}/100 ({score['label']})\n\n"
        "• /nearby — closest police & hospitals\n"
        "• /newtrip — plan a journey with arrival alerts\n"
        "• /timer 60 — alert my guardians if I don't check in"
    )


# ═════════════════════════════════════════════
#  Safety status, zones, nearby help, briefings
# ═════════════════════════════════════════════

async def _score_for(uid: int, state: dict, inside_zones: list[dict]) -> dict:
    location_known = bool(state["last_seen_ts"]) and time.time() - state["last_seen_ts"] < 3600
    return compute_safety_score(
        zone_risk=highest_risk(inside_zones),
        local_hour=approx_local_hour(state["last_lng"]) if state["last_lng"] is not None else None,
        open_incidents=len(await get_open_user_incidents(uid)),
        recent_anomalies=recent_anomaly_count(uid),
        checkin_pending=bool(state["checkin_pending_ts"]),
        has_digital_id=await get_active_tourist_id(uid) is not None,
        has_guardian=any(g["guardian_telegram_id"] for g in await get_guardians(uid)),
        location_known=location_known,
    )


async def safety_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    state = await get_safety_state(user.id)
    zones = await get_active_zones()
    inside = [z for z in zones if z["zone_id"] in state["zone_state"]["inside"]]
    from travel_buddy.bot.intel_handlers import build_live_context
    live = await build_live_context(user.id)  # adds weather + learned hotspots to the score
    score = live["_score"]
    tid = await get_active_tourist_id(user.id)
    guardians = [g for g in await get_guardians(user.id) if g["guardian_telegram_id"]]
    trip = await get_active_or_planning_trip(user.id)
    open_inc = await get_open_user_incidents(user.id)

    bar = "█" * (score["score"] // 10) + "░" * (10 - score["score"] // 10)
    lines = [
        "🛡️ <b>Your Safety Status</b>", "",
        f"{score['emoji']} <b>{score['score']}/100 — {score['label']}</b>",
        f"<code>{bar}</code>",
    ]
    if score["factors"]:
        lines += ["<i>" + escape("; ".join(score["factors"])) + "</i>"]
    lines += [
        "",
        f"📍 <b>Last location:</b> {_fmt_ago(state['last_seen_ts'])}",
        "🗺️ <b>Zone:</b> " + (", ".join(f"{RISK_EMOJI[z['risk_level']]} {escape(z['name'])}" for z in inside) or "🟢 none"),
        "🧳 <b>Trip:</b> " + (f"{escape(trip['destination_name'])} ({trip['status']})" if trip else "none"),
        "🪪 <b>Digital ID:</b> " + (f"<code>{tid['tourist_id']}</code>" if tid else "not registered → /registerid"),
        f"👨‍👩‍👧 <b>Guardians linked:</b> {len(guardians)}" + ("" if guardians else " → /addguardian"),
        f"🛰️ <b>Auto-monitoring:</b> {'on' if state['monitoring_enabled'] else 'off'}",
        "⏱️ <b>Safety timer:</b> " + (f"due in {int((state['timer_due_ts'] - time.time()) // 60)} min"
                                      if state["timer_due_ts"] else "off"),
        f"🚨 <b>Open incidents:</b> {len(open_inc)}",
    ]
    if live.get("place"):
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("📍"))
        lines.insert(idx, f"🌍 <b>Near:</b> {escape(live['place'])}")
    if live.get("_weather"):
        w = live["_weather"]
        lines.append(f"🌦️ <b>Weather:</b> {escape(w['summary'])}"
                     + ("" if not w["hazards"] else " — " + ", ".join(h["key"].replace("_", " ") for h in w["hazards"])))
    if live.get("nearby_hotspots"):
        lines.append(f"🔥 <b>Hotspots:</b> {escape(live['nearby_hotspots'])}")
    if state["checkin_pending_ts"]:
        lines.append("\n❗ <b>A safety check-in is waiting for you</b> — send /safe if you're okay.")
    await update.effective_message.reply_html("\n".join(lines))


async def zones_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lat, lng, _ = await _last_known_location(update.effective_user.id)
    zones = await get_active_zones()
    if lat is not None:
        items = zones_near(lat, lng, zones, within_m=50000)
        title = "🗺️ <b>Risk zones within 50 km of you</b>"
    else:
        items = [(z, None) for z in zones]
        title = "🗺️ <b>All monitored risk zones</b> <i>(share a location to see distances)</i>"
    if not items:
        await update.effective_message.reply_html("🟢 No monitored risk zones within 50 km of your last location.")
        return
    lines = [title, ""]
    for z, edge in items[:15]:
        where = "" if edge is None else (" — <b>you are inside</b>" if edge <= 0 else f" — {edge / 1000:.1f} km away")
        lines.append(f"{RISK_EMOJI[z['risk_level']]} <code>{z['zone_id']}</code> <b>{escape(z['name'])}</b> "
                     f"({z['risk_level']}, r={z['radius_m'] / 1000:.1f} km){where}")
    await update.effective_message.reply_html("\n".join(lines))


async def _emergency_block(lat: float | None, lng: float | None) -> str:
    country = None
    if lat is not None:
        geo = await reverse_geocode(lat, lng)
        country = geo["country_code"] if geo else None
    numbers = emergency_numbers_for(country)
    return "☎️ <b>Emergency numbers</b>" + (f" ({country.upper()})" if country else "") + "\n" + \
        "\n".join(f"• {k}: <b>{v}</b>" for k, v in numbers.items())


async def nearby_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lat, lng, seen = await _last_known_location(update.effective_user.id)
    if lat is None:
        await update.effective_message.reply_html(
            "📍 I don't know where you are yet. Send your location (📎 → Location) and then tap /nearby again."
        )
        return
    status = await update.effective_message.reply_html("🔎 <i>Finding the nearest police stations and hospitals…</i>")
    services = await find_nearby_services(lat, lng)
    lines = [f"🏥 <b>Nearest help</b> <i>(from your location {_fmt_ago(seen)})</i>", ""]
    if services:
        for s in services:
            phone = f" · ☎️ <code>{escape(s['phone'])}</code>" if s["phone"] else ""
            lines.append(f"{SERVICE_LABELS.get(s['type'], s['type'])}: <a href='{maps_link(s['lat'], s['lng'])}'>"
                         f"{escape(s['name'])}</a> — {s['distance_m'] / 1000:.1f} km{phone}")
    else:
        lines.append("<i>Couldn't load nearby services right now (map service busy or none within 5 km).</i>")
    lines += ["", await _emergency_block(lat, lng)]
    await status.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def safetytips_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    place = " ".join(context.args).strip() if context.args else ""
    if not place:
        trip = await get_active_or_planning_trip(update.effective_user.id)
        place = trip["destination_name"] if trip else ""
    if not place:
        await update.effective_message.reply_html("Usage: <code>/safetytips Shillong</code> (or plan a trip first)")
        return
    status = await update.effective_message.reply_html(f"🛡️ <i>Preparing an AI safety briefing for {escape(place)}…</i>")
    text = await generate_safety_briefing(place)
    try:
        await status.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await status.edit_text(re.sub(r"<[^>]+>", "", text), disable_web_page_preview=True)


async def timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await upsert_user(uid, update.effective_user.username or update.effective_user.first_name)
    arg = context.args[0].lower() if context.args else ""
    if arg in ("off", "stop", "cancel"):
        await update_safety_state(uid, timer_due_ts=None)
        await update.effective_message.reply_text("⏱️ Safety timer cancelled.")
        return
    if not arg.isdigit() or not 5 <= int(arg) <= 720:
        await update.effective_message.reply_html(
            "⏱️ <b>Safety timer</b> — going on a hike, a night out or a solo walk?\n"
            "<code>/timer 90</code> → if you don't check in within 90 min, I'll ask if you're okay and then "
            "alert your guardians.\n<code>/timer off</code> to cancel. (5–720 minutes)"
        )
        return
    minutes = int(arg)
    await update_safety_state(uid, timer_due_ts=time.time() + minutes * 60)
    guardians = [g for g in await get_guardians(uid) if g["guardian_telegram_id"]]
    await update.effective_message.reply_html(
        f"⏱️ Safety timer set for <b>{minutes} min</b>. Send /safe when you're back.\n"
        + ("" if guardians else "⚠️ <i>No guardian linked yet — escalations will only reach the safety desk. /addguardian</i>")
    )


async def _mark_safe(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int) -> str:
    return mark_safe_text(await mark_safe(context.bot, uid))


async def safe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _mark_safe(update, context, update.effective_user.id)
    await update.effective_message.reply_html(text)


async def monitoring_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = await get_safety_state(uid)
        await update.effective_message.reply_html(
            f"🛰️ Auto-monitoring is <b>{'on' if state['monitoring_enabled'] else 'off'}</b>.\n"
            "It detects sudden halts, long stops, route deviation and lost signal during trips.\n"
            "Use <code>/monitoring on</code> or <code>/monitoring off</code>."
        )
        return
    await update_safety_state(uid, monitoring_enabled=1 if arg == "on" else 0)
    if arg == "off":
        reset_motion(uid)
    await update.effective_message.reply_text(f"🛰️ Auto-monitoring turned {arg}.")


# ═════════════════════════════════════════════
#  SOS & incident reporting
# ═════════════════════════════════════════════

async def sos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    incident = await trigger_sos(context.bot, user.id)
    lat, lng, fresh = incident["lat"], incident["lng"], incident["fresh"]
    reach = (f"✅ <b>{incident['delivered']}</b> guardian(s)/responder(s) alerted."
             if incident["delivered"] else
             "⚠️ No guardian or safety desk could be reached — <b>call the emergency number below now.</b> "
             "Add guardians with /addguardian.")
    loc_note = ("📍 Your location was attached." if fresh else
                "📍 <b>Share your live location now</b> (📎 → Location → Share Live Location) so responders can find you.")
    await update.effective_message.reply_html(
        f"🆘 <b>SOS SENT — incident #{incident['incident_id']}</b>\n\n{reach}\n{loc_note}\n\n"
        + await _emergency_block(lat, lng) +
        "\n\nStay in a safe, public place if you can. Send /safe once you're okay.",
        reply_markup=MAIN_KEYBOARD,
    )
    if lat is not None:
        services = [s for s in await find_nearby_services(lat, lng, radius_m=5000, per_type=1)
                    if s["type"] in ("police", "hospital")]
        if services:
            await update.effective_message.reply_html(
                "🏃 <b>Closest help:</b>\n" + "\n".join(
                    f"{SERVICE_LABELS[s['type']]}: <a href='{maps_link(s['lat'], s['lng'])}'>{escape(s['name'])}</a>"
                    f" — {s['distance_m'] / 1000:.1f} km" for s in services),
                disable_web_page_preview=True,
            )


async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    context.user_data["report"] = {}
    keys = list(REPORT_TYPES)
    await update.effective_message.reply_html(
        "📝 <b>Report an incident</b>\n\nWhat happened? <i>(/cancel to abort — for emergencies use /sos)</i>",
        reply_markup=ReplyKeyboardMarkup([keys[i:i + 2] for i in range(0, len(keys), 2)],
                                         one_time_keyboard=True, resize_keyboard=True),
    )
    return REP_TYPE


async def report_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["report"]["type"] = REPORT_TYPES.get(text, "other")
    await update.message.reply_html(
        "Describe what happened in a few words (where, when, anyone hurt?):",
        reply_markup=MAIN_KEYBOARD,
    )
    return REP_DESC


async def report_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["report"]["description"] = update.message.text.strip()[:1000]
    await update.message.reply_html(
        "📍 Where did it happen? Send a location pin, or choose:",
        reply_markup=ReplyKeyboardMarkup([["📌 Use my last known location"], ["⏭️ Skip location"]],
                                         one_time_keyboard=True, resize_keyboard=True),
    )
    return REP_LOC


async def report_location_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    loc = update.message.location
    await update_safety_state(update.effective_user.id, last_lat=loc.latitude, last_lng=loc.longitude,
                              last_seen_ts=time.time())
    return await _finish_report(update, context, loc.latitude, loc.longitude)


async def report_location_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lat = lng = None
    if "last" in update.message.text.lower():
        lat, lng, _ = await _last_known_location(update.effective_user.id)
    return await _finish_report(update, context, lat, lng)


async def _finish_report(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         lat: float | None, lng: float | None) -> int:
    report = context.user_data.pop("report", {})
    status = await update.effective_message.reply_html("🤖 <i>AI is triaging your report…</i>")
    triage = await classify_incident(report.get("description", ""), report.get("type"))
    incident = await raise_incident(
        context.bot, update.effective_user.id, triage["category"], triage["severity"],
        report.get("description", ""), lat, lng, source="report", ai_advice=triage["advice"],
    )
    who = (f"{incident['delivered']} responder(s) alerted." if incident["delivered"]
           else "Logged (low severity — responders not paged)." if triage["severity"] == "low"
           else "No guardian/safety desk linked to alert — /addguardian.")
    await _replace_status(
        update, status,
        f"✅ <b>Incident #{incident['incident_id']} recorded</b>\n\n"
        f"<b>Type:</b> {TYPE_LABEL.get(triage['category'], triage['category'])}\n"
        f"<b>Severity:</b> {SEVERITY_EMOJI[triage['severity']]} {triage['severity'].upper()} "
        f"<i>(AI triage: {triage['engine']})</i>\n"
        f"<b>Ledger:</b> block #{incident['block_index']} · <code>{incident['block_hash'][:16]}…</code>\n"
        f"📣 {who}\n\n"
        f"💡 <b>What to do now:</b>\n{escape(triage['advice'])}\n\n"
        f"📄 Need a police report? → <code>/efir {incident['incident_id']}</code>\n"
        + ("🚨 <b>If anyone is in danger, press /sos now.</b>" if triage["severity"] in ("high", "critical") else ""),
    )
    return ConversationHandler.END


async def _replace_status(update: Update, status, text: str) -> None:
    """Swap a temporary 'working…' message for the final result and restore the SOS keyboard.
    (Telegram can't attach a reply keyboard to an edited message, so we delete and resend.)"""
    try:
        await status.delete()
    except Exception:
        pass
    await update.effective_message.reply_html(text, reply_markup=MAIN_KEYBOARD, disable_web_page_preview=True)


async def incidents_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    show_all = context.args and context.args[0].lower() == "all"
    if show_all and not is_admin(uid):
        await update.effective_message.reply_text("⛔ Only the safety desk (ADMIN_IDS) can view all incidents.")
        return
    rows = await get_open_incidents(20) if show_all else await get_user_incidents(uid, 10)
    if not rows:
        await update.effective_message.reply_text("No incidents on record. 🙌")
        return
    title = "🚨 <b>Open incidents (all tourists)</b>" if show_all else "🗂️ <b>Your incidents</b>"
    lines = [title, ""]
    for r in rows:
        status = {"open": "🔴 open", "acknowledged": "🟡 acknowledged", "resolved": "🟢 resolved"}[r["status"]]
        loc = f" · <a href='{maps_link(r['lat'], r['lng'])}'>map</a>" if r["lat"] is not None else ""
        owner = f" · user <code>{r['telegram_id']}</code>" if show_all else ""
        lines.append(f"#{r['incident_id']} {SEVERITY_EMOJI[r['severity']]} {TYPE_LABEL.get(r['incident_type'], r['incident_type'])}"
                     f" — {status}{loc}{owner}\n   <i>{_fmt_date(r['created_at'])}</i>")
    lines.append("\nClose with <code>/resolve ID</code> · police draft with <code>/efir ID</code>")
    await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def _can_manage(uid: int, incident: dict) -> bool:
    return await can_manage(uid, incident)


async def resolve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.effective_message.reply_html("Usage: <code>/resolve ID</code>")
        return
    inc = await get_incident(int(context.args[0].lstrip("#")))
    if not inc or not await _can_manage(uid, inc):
        await update.effective_message.reply_text("Incident not found or you don't have access to it.")
        return
    await _resolve_and_announce(context, inc, uid)
    await update.effective_message.reply_html(f"🟢 Incident #{inc['incident_id']} resolved and recorded on the ledger.")


async def _resolve_and_announce(context: ContextTypes.DEFAULT_TYPE, inc: dict, by_uid: int) -> None:
    await resolve_as(context.bot, inc, by_uid)


async def efir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.effective_message.reply_html("Usage: <code>/efir ID</code> — drafts a police complaint (E-FIR) for an incident.")
        return
    inc = await get_incident(int(context.args[0].lstrip("#")))
    if not inc or not await _can_manage(uid, inc):
        await update.effective_message.reply_text("Incident not found or you don't have access to it.")
        return
    text = await build_efir_text(inc)
    await update.effective_message.reply_document(
        document=io.BytesIO(text.encode("utf-8")),
        filename=f"EFIR_draft_incident_{inc['incident_id']}.txt",
        caption=f"📄 E-FIR draft for incident #{inc['incident_id']}. Review, print and submit to the police.",
    )


# ═════════════════════════════════════════════
#  Inline button callbacks (check-ins & responders)
# ═════════════════════════════════════════════

async def safety_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    data = query.data or ""

    if data == "ci:safe":
        text = await _mark_safe(update, context, uid)
        await query.answer("Marked safe ✅")
        await query.edit_message_reply_markup(None)
        await query.message.reply_html(text)
    elif data == "ci:help":
        await query.answer("Sending SOS…")
        await query.edit_message_reply_markup(None)
        await update_safety_state(uid, checkin_pending_ts=None, checkin_reason=None)
        await sos_handler(update, context)
    elif data.startswith("inc:"):
        _, action, inc_id = data.split(":")
        inc = await get_incident(int(inc_id))
        if not inc or not await _can_manage(uid, inc):
            await query.answer("Not authorised for this incident.", show_alert=True)
            return
        if action == "ack":
            await acknowledge_as(context.bot, inc, uid, query.from_user.full_name)
            await query.answer("Acknowledged")
            await query.message.reply_html(f"👀 You acknowledged incident #{inc['incident_id']}.")
        elif action == "res":
            await _resolve_and_announce(context, inc, uid)
            await query.answer("Resolved")
            await query.edit_message_reply_markup(None)
            await query.message.reply_html(f"🟢 Incident #{inc['incident_id']} resolved.")
    else:
        await query.answer()


# ═════════════════════════════════════════════
#  Authority / safety desk tools
# ═════════════════════════════════════════════

async def dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_html(
            f"⛔ The dashboard is for the safety desk. Ask the operator to add your ID "
            f"<code>{uid}</code> to <code>ADMIN_IDS</code> in .env."
        )
        return
    users = (await _fetchone("SELECT COUNT(*) AS n FROM users"))["n"]
    ids = (await _fetchone("SELECT COUNT(*) AS n FROM tourist_ids WHERE status = 'active'"))["n"]
    active_trips = (await _fetchone("SELECT COUNT(*) AS n FROM trips WHERE status = 'active'"))["n"]
    open_by_sev = await _fetchall(
        "SELECT severity, COUNT(*) AS n FROM incidents WHERE status != 'resolved' GROUP BY severity")
    sev = {r["severity"]: r["n"] for r in open_by_sev}
    pending = (await _fetchone("SELECT COUNT(*) AS n FROM safety_state WHERE checkin_pending_ts IS NOT NULL"))["n"]
    live = (await _fetchone("SELECT COUNT(*) AS n FROM safety_state WHERE last_seen_ts >= ?",
                            (time.time() - 3600,)))["n"]

    zones = {z["zone_id"]: z for z in await get_active_zones()}
    in_risk = []
    for s in await _fetchall("SELECT telegram_id, zone_state FROM safety_state"):
        for zid in json.loads(s["zone_state"] or "{}").get("inside", []):
            if zid in zones and RISK_ORDER[zones[zid]["risk_level"]] >= RISK_ORDER["high"]:
                in_risk.append((s["telegram_id"], zones[zid]))
    chain = await verify_chain()

    lines = [
        "🖥️ <b>Tourist Safety Command Dashboard</b>", "",
        f"👥 Registered users: <b>{users}</b> · 🪪 Active digital IDs: <b>{ids}</b>",
        f"🧳 Active trips: <b>{active_trips}</b> · 📡 Seen in last hour: <b>{live}</b>",
        f"🚨 Open incidents: 🔴 {sev.get('critical', 0)} · 🟠 {sev.get('high', 0)} · "
        f"🟡 {sev.get('medium', 0)} · 🔵 {sev.get('low', 0)}",
        f"⏳ Unanswered check-ins: <b>{pending}</b>",
        f"🚧 Tourists in high/restricted zones: <b>{len(in_risk)}</b>",
        f"⛓️ Ledger: {chain['length']} blocks — " + ("✅ intact" if chain["valid"] else f"❌ BROKEN at #{chain['broken_at']}"),
    ]
    for tg_id, z in in_risk[:5]:
        name, tid, _ = await tourist_label(tg_id)
        lines.append(f"   • {escape(name)} {('(' + tid + ')') if tid else ''} in {RISK_EMOJI[z['risk_level']]} {escape(z['name'])}")
    top = await get_open_incidents(5)
    if top:
        lines += ["", "<b>Priority queue:</b>"]
        for r in top:
            name, tid, _ = await tourist_label(r["telegram_id"])
            loc = f" <a href='{maps_link(r['lat'], r['lng'])}'>📍</a>" if r["lat"] is not None else ""
            lines.append(f"#{r['incident_id']} {SEVERITY_EMOJI[r['severity']]} {TYPE_LABEL.get(r['incident_type'], r['incident_type'])}"
                         f" — {escape(name)}{loc} · {r['status']}")
    lines.append("\n<code>/incidents all</code> · <code>/whereis TID</code> · <code>/addzone</code> · <code>/resolve ID</code>")
    await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def addzone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Only the safety desk can add zones.")
        return
    usage = ("Usage: <code>/addzone RISK RADIUS_M LAT LNG Name | advisory</code>\n"
             "RISK = safe, caution, high, restricted\n"
             "e.g. <code>/addzone restricted 2000 25.5788 91.8933 Army Cantonment | No entry for civilians</code>")
    raw = " ".join(context.args or [])
    m = re.match(r"^(safe|caution|high|restricted)\s+(\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(.+)$",
                 raw.strip(), re.IGNORECASE)
    if not m:
        await update.effective_message.reply_html(usage)
        return
    risk, radius, lat, lng, rest = m.groups()
    name, _, advisory = rest.partition("|")
    lat, lng, radius = float(lat), float(lng), float(radius)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 50 <= radius <= 200000):
        await update.effective_message.reply_html("Coordinates or radius out of range (radius 50 m – 200 km).\n" + usage)
        return
    zone_id = await add_zone(name.strip()[:80], lat, lng, radius, risk.lower(), advisory.strip()[:300], uid)
    await update.effective_message.reply_html(
        f"{RISK_EMOJI[risk.lower()]} Zone <code>{zone_id}</code> <b>{escape(name.strip())}</b> added "
        f"({risk.lower()}, {radius / 1000:.1f} km). Tourists will be alerted on approach/entry."
    )


async def removezone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Only the safety desk can remove zones.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_html("Usage: <code>/removezone ZONE_ID</code> (see /zones)")
        return
    await deactivate_zone(int(context.args[0]))
    await update.effective_message.reply_text(f"Zone {context.args[0]} deactivated.")


async def whereis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admins: /whereis TID-XXXX or a Telegram ID. Guardians: /whereis (lists the people they guard)."""
    uid = update.effective_user.id
    targets: list[int] = []
    if context.args:
        arg = context.args[0].strip()
        if arg.upper().startswith("TID-"):
            rec = await get_tourist_id(arg)
            if rec:
                targets = [rec["telegram_id"]]
        elif arg.isdigit():
            targets = [int(arg)]
        if targets and not (is_admin(uid) or await is_guardian_of(uid, targets[0])):
            await update.effective_message.reply_text("⛔ You can only locate tourists you are a guardian for.")
            return
    else:
        targets = await get_tourists_guarded_by(uid)
    if not targets:
        await update.effective_message.reply_html(
            "Nobody to show. Guardians: use /whereis to see people you guard. "
            "Safety desk: <code>/whereis TID-XXXX</code>."
        )
        return
    for t in targets[:5]:
        name, tid, _ = await tourist_label(t)
        s = await get_safety_state(t)
        trip = await get_active_or_planning_trip(t)
        open_inc = await get_open_user_incidents(t)
        lines = [f"👤 <b>{escape(name)}</b>" + (f" (<code>{tid}</code>)" if tid else "")]
        if s["last_lat"] is not None:
            lines.append(f"📍 <a href='{maps_link(s['last_lat'], s['last_lng'])}'>{s['last_lat']:.5f}, "
                         f"{s['last_lng']:.5f}</a> · {_fmt_ago(s['last_seen_ts'])}")
        else:
            lines.append("📍 No location shared yet")
        lines.append("🧳 Trip: " + (f"{escape(trip['destination_name'])} ({trip['status']})" if trip else "none"))
        lines.append(f"🚨 Open incidents: {len(open_inc)}"
                     + ("  ⏳ check-in pending" if s["checkin_pending_ts"] else ""))
        await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)
        if s["last_lat"] is not None:
            await update.effective_message.reply_location(latitude=s["last_lat"], longitude=s["last_lng"])


# ═════════════════════════════════════════════
#  Quick-access keyboard buttons
# ═════════════════════════════════════════════

async def quick_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persistent keyboard buttons work everywhere, even in the middle of another flow."""
    text = update.message.text.strip()
    context.args = []
    if text == BTN_SOS:
        await sos_handler(update, context)
    elif text == BTN_SAFETY:
        await safety_status_handler(update, context)
    elif text == BTN_NEARBY:
        await nearby_handler(update, context)
    elif text == BTN_MYID:
        await myid_handler(update, context)
    raise ApplicationHandlerStop
