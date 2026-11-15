"""
intel_handlers.py — Next-generation features of Travel Buddy Safety.

  🤖 AI Safety Copilot    /ask + any plain message → answers using the traveller's live situation
  🎙️ Voice SOS / reports  voice note → Whisper transcription → distress detection → SOS or copilot
  📸 Evidence vault       photo → SHA-256 sealed on the ledger → attached to incident → responders
  🧭 Safe-route analysis  /saferoute <place> → road route vs zones, hotspots, weather, night
  🌩️ Weather hazards      /weather (plus automatic severe-weather alerts from the monitor)
  🔥 Learned hotspots     /hotspots, and proactive warnings when walking into one
  🗣️ Phrasebook           /phrases [language] [custom text]
  📢 Community alerts     /community on|off
  📱 QR Digital ID        QR code for /myid that opens the public verification page
  🖥️ Web command center   /web
"""

import hashlib
import io
import json
import logging
import re
import time
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from travel_buddy.bot import safety_handlers as sh
from travel_buddy.ai.copilot import (
    COUNTRY_LANGUAGE, PHRASEBOOK, answer_copilot, detect_distress, phrasebook_text,
    transcribe_voice, translate_phrase,
)
from travel_buddy.ai.triage import classify_incident, keyword_triage
from travel_buddy.safety.ledger import append_block, get_active_tourist_id
from travel_buddy.core.database import get_active_or_planning_trip, upsert_user
from travel_buddy.core.geocoding import geocode_location
from travel_buddy.safety.geofence import RISK_EMOJI, highest_risk, maps_link, reverse_geocode
from travel_buddy.safety.incidents import forward_media, is_admin, raise_incident, TYPE_LABEL, SEVERITY_EMOJI
from travel_buddy.safety.risk_intel import (
    HOTSPOT_WARN_M, assess_route, directions_link, fetch_route, get_hotspots,
    get_weather_risk, hotspots_near,
)
from travel_buddy.safety.store import (
    add_evidence, get_active_zones, get_open_user_incidents, get_safety_state,
    update_safety_state,
)
from travel_buddy.safety.monitor import approx_local_hour
from travel_buddy.web.dashboard import dashboard_token, public_base_url

logger = logging.getLogger(__name__)

HOTSPOT_REWARN_S = 6 * 3600
EVIDENCE_WINDOW_S = 24 * 3600
HOTSPOT_TIPS = {
    "theft": "Keep bags zipped and in front of you; don't flash phones or cash.",
    "scam": "Agree prices first; use registered guides/taxis only.",
    "harassment": "Stay in busy, well-lit areas; move in a group if possible.",
    "accident": "Extra care crossing roads and on vehicles here.",
    "natural_hazard": "Hazard-prone ground — keep to marked, safe paths.",
}


def sos_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🆘 Send SOS now", callback_data="ci:help"),
        InlineKeyboardButton("✅ I'm okay", callback_data="ci:safe"),
    ]])


def _short_place(display_name: str | None) -> str:
    if not display_name:
        return ""
    return ", ".join(p.strip() for p in display_name.split(",")[:3])


def _local_hour(weather: dict | None, lng: float | None) -> float | None:
    if weather and weather.get("utc_offset_seconds") is not None:
        t = time.gmtime(time.time() + weather["utc_offset_seconds"])
        return t.tm_hour + t.tm_min / 60
    return approx_local_hour(lng) if lng is not None else None


# ═════════════════════════════════════════════
#  Live context (shared by copilot & status)
# ═════════════════════════════════════════════

async def build_live_context(uid: int) -> dict:
    state = await get_safety_state(uid)
    lat, lng = state["last_lat"], state["last_lng"]
    zones = await get_active_zones()
    inside = [z for z in zones if z["zone_id"] in state["zone_state"]["inside"]]
    ctx: dict = {}
    weather = None
    hot_risk = None
    if lat is not None:
        geo = await reverse_geocode(lat, lng)
        weather = await get_weather_risk(lat, lng)
        near = hotspots_near(lat, lng, await get_hotspots(), within_m=1000)
        if near:
            hot_risk = near[0][0]["risk_level"]
        ctx.update({
            "place": _short_place(geo["display_name"]) if geo else f"{lat:.4f}, {lng:.4f}",
            "country": (geo or {}).get("country_code"),
            "last_location_age": sh._fmt_ago(state["last_seen_ts"]),
            "weather": weather["summary"] if weather else None,
            "weather_hazards": "; ".join(h["text"] for h in weather["hazards"]) if weather else None,
            "nearby_hotspots": "; ".join(
                f"{h['count']} recent {h['dominant_type']} reports {max(0, d):.0f} m away" for h, d in near[:3]),
        })
        hour = _local_hour(weather, lng)
        if hour is not None:
            ctx["local_time"] = f"{int(hour):02d}:{int((hour % 1) * 60):02d}"
    score = await sh._score_for(uid, state, inside)
    if weather or hot_risk:
        score = await _score_with_intel(uid, state, inside, weather, hot_risk)
    trip = await get_active_or_planning_trip(uid)
    ctx.update({
        "safety_score": f"{score['score']}/100 ({score['label']})",
        "zone": ", ".join(f"{z['name']} ({z['risk_level']})" for z in inside) or "no risk zone",
        "trip": f"to {trip['destination_name']} ({trip['status']}, {trip['transport_mode']})" if trip else None,
        "open_incidents": len(await get_open_user_incidents(uid)) or None,
        "has_digital_id": bool(await get_active_tourist_id(uid)),
    })
    ctx["_score"] = score
    ctx["_weather"] = weather
    return ctx


async def _score_with_intel(uid, state, inside, weather, hot_risk) -> dict:
    from travel_buddy.safety.monitor import compute_safety_score, recent_anomaly_count
    from travel_buddy.safety.store import get_guardians
    return compute_safety_score(
        zone_risk=highest_risk(inside),
        local_hour=_local_hour(weather, state["last_lng"]),
        open_incidents=len(await get_open_user_incidents(uid)),
        recent_anomalies=recent_anomaly_count(uid),
        checkin_pending=bool(state["checkin_pending_ts"]),
        has_digital_id=await get_active_tourist_id(uid) is not None,
        has_guardian=any(g["guardian_telegram_id"] for g in await get_guardians(uid)),
        location_known=bool(state["last_seen_ts"]) and time.time() - state["last_seen_ts"] < 3600,
        weather_risk=(weather or {}).get("worst", "none"),
        hotspot_risk=hot_risk,
    )


# ═════════════════════════════════════════════
#  🤖 AI Safety Copilot
# ═════════════════════════════════════════════

async def _copilot_reply(update: Update, question: str, prefix: str = "") -> None:
    uid = update.effective_user.id
    if detect_distress(question) or keyword_triage(question)["severity"] == "critical":
        await update.effective_message.reply_html(
            prefix + "🚨 <b>This sounds like an emergency.</b>\nTap <b>Send SOS</b> to alert your guardians and "
            "the safety desk with your location — or call <b>112</b> right now.",
            reply_markup=sos_keyboard(),
        )
        return
    status = await update.effective_message.reply_html(prefix + "🤖 <i>Thinking about your situation…</i>")
    ctx = await build_live_context(uid)
    public_ctx = {k: v for k, v in ctx.items() if not k.startswith("_")}
    answer = await answer_copilot(question, public_ctx)
    try:
        await status.edit_text(prefix + answer, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await status.edit_text(prefix + re.sub(r"<[^>]+>", "", answer), disable_web_page_preview=True)


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    question = " ".join(context.args or []).strip()
    if not question:
        await update.effective_message.reply_html(
            "🤖 <b>AI Safety Copilot</b>\nAsk anything — I use your live location, zone, weather and nearby "
            "incident hotspots.\n\n• <code>/ask is it safe to walk back to my hotel now?</code>\n"
            "• <code>/ask what should I avoid around here at night?</code>\n\n"
            "💡 You can also just type a message, or send a 🎙️ voice note in any language."
        )
        return
    await _copilot_reply(update, question)


async def free_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain messages outside any flow go to the copilot."""
    text = (update.message.text or "").strip()
    if not text:
        return
    await upsert_user(update.effective_user.id, update.effective_user.username or update.effective_user.first_name)
    await _copilot_reply(update, text)


# ═════════════════════════════════════════════
#  🎙️ Voice SOS & 📸 photo evidence
# ═════════════════════════════════════════════

async def _download(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


async def _seal_evidence(uid: int, incident_id: int | None, kind: str, file_id: str,
                         data: bytes, transcript: str | None = None) -> tuple[str, dict]:
    digest = hashlib.sha256(data).hexdigest()
    block = await append_block("EVIDENCE", {"incident_id": incident_id, "kind": kind, "sha256": digest})
    await add_evidence(incident_id, uid, kind, file_id, digest, transcript, block["hash"])
    return digest, block


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    voice = update.message.voice or update.message.audio
    status = await update.message.reply_html("🎙️ <i>Listening…</i>")
    data = await _download(context, voice.file_id)
    result = await transcribe_voice(data, "voice.ogg")

    if not result["ok"] or not result["text"]:
        await status.edit_text(
            "🎙️ I couldn't understand that voice note"
            + (" (voice AI needs GROQ_API_KEY)" if "GROQ" in (result.get("error") or "") else "")
            + ".\nIf you are in danger, tap SOS:",
            reply_markup=sos_keyboard(),
        )
        return

    text = result["text"]
    lang = f" [{result['language']}]" if result.get("language") else ""
    heard = f"🎙️ <i>Heard{escape(lang)}:</i> “{escape(text[:300])}”\n\n"

    if detect_distress(text) or keyword_triage(text)["severity"] == "critical":
        lat, lng, seen = await sh._last_known_location(user.id)
        incident = await raise_incident(
            context.bot, user.id, "sos", "critical", f"Voice SOS: “{text[:400]}”",
            lat, lng, source="voice", notify_guardians=True, notify_admins=True,
        )
        digest, _ = await _seal_evidence(user.id, incident["incident_id"], "voice", voice.file_id, data, text)
        await forward_media(context.bot, user.id, "voice", voice.file_id,
                            f"🎙️ Voice SOS · incident #{incident['incident_id']}\n“{escape(text[:300])}”")
        await status.edit_text(
            heard + f"🆘 <b>VOICE SOS SENT — incident #{incident['incident_id']}</b>\n"
            f"✅ {incident['delivered']} responder(s) alerted and your voice note was forwarded.\n"
            f"🔐 Recording fingerprint sealed on ledger: <code>{digest[:16]}…</code>\n\n"
            + await sh._emergency_block(lat, lng)
            + "\n\nShare live location now (📎 → Location). Send /safe once you're okay.",
            parse_mode="HTML",
        )
        return

    await status.delete()
    await _copilot_reply(update, text, prefix=heard)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await upsert_user(user.id, user.username or user.first_name)
    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()

    # Attach to the most recent open incident from the last 24 h, else open one from the caption.
    target = None
    for inc in await get_open_user_incidents(user.id):
        try:
            from datetime import datetime, timezone
            age = time.time() - datetime.fromisoformat(inc["created_at"]).replace(
                tzinfo=timezone.utc).timestamp()
        except Exception:
            age = 0
        if age <= EVIDENCE_WINDOW_S and inc["incident_type"] not in ("no_response",):
            target = inc
            break

    if target is None and not caption:
        await update.message.reply_html(
            "📸 To log this photo as evidence, resend it with a <b>caption</b> describing what happened "
            "(e.g. <i>“this taxi driver took my bag”</i>), or start with /report."
        )
        return

    status = await update.message.reply_html("🔐 <i>Sealing photo evidence on the ledger…</i>")
    data = await _download(context, photo.file_id)
    lat, lng, _ = await sh._last_known_location(user.id)

    created = False
    if target is None:
        triage = await classify_incident(caption)
        target = await raise_incident(
            context.bot, user.id, triage["category"], triage["severity"], caption,
            lat, lng, source="photo", ai_advice=triage["advice"],
        )
        created = True

    digest, block = await _seal_evidence(user.id, target["incident_id"], "photo", photo.file_id, data, caption or None)
    forwarded = 0
    if target["severity"] != "low" or target["incident_type"] == "sos":
        forwarded = await forward_media(
            context.bot, user.id, "photo", photo.file_id,
            f"📸 Evidence for incident #{target['incident_id']} "
            f"({TYPE_LABEL.get(target['incident_type'], target['incident_type'])})"
            + (f"\n“{escape(caption[:300])}”" if caption else ""),
        )
    await status.edit_text(
        ("🆕 New incident " if created else "📎 Attached to incident ")
        + f"<b>#{target['incident_id']}</b> {SEVERITY_EMOJI[target['severity']]} "
        f"{TYPE_LABEL.get(target['incident_type'], target['incident_type'])}\n"
        f"🔐 SHA-256 <code>{digest[:24]}…</code> sealed in block #{block['block_index']}\n"
        f"📤 Forwarded to {forwarded} responder(s).\n\n"
        "This fingerprint proves the photo hasn't been altered — useful for police and insurance "
        f"(<code>/efir {target['incident_id']}</code>).",
        parse_mode="HTML",
    )


# ═════════════════════════════════════════════
#  🧭 Safe-route analysis
# ═════════════════════════════════════════════

async def saferoute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    dest_q = " ".join(context.args or []).strip()
    if not dest_q:
        trip = await get_active_or_planning_trip(uid)
        if trip:
            dest = (trip["destination_lat"], trip["destination_lng"], trip["destination_name"])
        else:
            await update.effective_message.reply_html(
                "🧭 <b>Safe-route check</b>\n<code>/saferoute Hawa Mahal, Jaipur</code>\n"
                "I'll check the real road route against risk zones, learned incident hotspots, weather and time of day."
            )
            return
    else:
        dest = None
    lat, lng, _ = await sh._last_known_location(uid)
    if lat is None:
        await update.effective_message.reply_html("📍 Share your location first (📎 → Location), then run /saferoute again.")
        return

    status = await update.effective_message.reply_html("🧭 <i>Analysing route safety…</i>")
    if dest is None:
        g = await geocode_location(dest_q)
        if not g:
            await status.edit_text(f"❌ Couldn't find “{dest_q}”. Try a more specific name.")
            return
        dest = (g[0], g[1], dest_q)
    b_lat, b_lng, b_name = dest

    route = await fetch_route(lat, lng, b_lat, b_lng)
    weather = await get_weather_risk(b_lat, b_lng)
    result = assess_route(route["path"], await get_active_zones(), await get_hotspots(),
                          local_hour=_local_hour(weather, b_lng), weather=weather)

    bar = "█" * (result["score"] // 10) + "░" * (10 - result["score"] // 10)
    mins = int(route["duration_s"] // 60)
    lines = [
        f"🧭 <b>Route safety → {escape(b_name)}</b>",
        f"{route['distance_m'] / 1000:.1f} km · ~{mins} min by road"
        + (" <i>(straight-line estimate)</i>" if route["source"] != "road" else ""),
        "",
        f"<b>Route score: {result['score']}/100</b>  <code>{bar}</code>",
        result["verdict"],
    ]
    if result["reasons"]:
        lines += ["", "<b>Why:</b>"] + [f"• {escape(r)}" for r in result["reasons"]]
    if weather:
        lines += ["", f"🌦️ Destination weather: {escape(weather['summary'])}"]
    lines += ["", f"🗺️ <a href='{directions_link(lat, lng, b_lat, b_lng)}'>Open directions</a>"]
    timer_min = max(15, int(mins * 1.5) + 15)
    await status.edit_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
            f"⏱️ Safety timer for this trip ({timer_min} min)", callback_data=f"tm:{timer_min}")]]),
    )


async def timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    minutes = int(q.data.split(":")[1])
    await update_safety_state(q.from_user.id, timer_due_ts=time.time() + minutes * 60)
    await q.answer(f"Timer set: {minutes} min")
    await q.edit_message_reply_markup(None)
    await q.message.reply_html(f"⏱️ Safety timer set for <b>{minutes} min</b>. Send /safe when you arrive.")


# ═════════════════════════════════════════════
#  🌩️ Weather, 🔥 hotspots, 🗣️ phrases, 📢 community
# ═════════════════════════════════════════════

async def weather_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lat, lng, _ = await sh._last_known_location(update.effective_user.id)
    if lat is None:
        await update.effective_message.reply_html("📍 Share your location first (📎 → Location), then /weather.")
        return
    w = await get_weather_risk(lat, lng)
    if not w:
        await update.effective_message.reply_text("🌦️ Weather service unavailable right now — try again shortly.")
        return
    lines = ["🌦️ <b>Weather & hazards near you</b>", f"Now: {escape(w['summary'])}", ""]
    lines += [f"• {h['text']}" for h in w["hazards"]] or ["✅ No weather hazards in the next 6 hours."]
    await update.effective_message.reply_html("\n".join(lines))


async def hotspots_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    hs = [h for h in await get_hotspots(force=True) if h["risk_level"] != "safe"]
    lat, lng, _ = await sh._last_known_location(uid)
    if not hs:
        await update.effective_message.reply_html(
            "🔥 No incident hotspots yet. The system learns them automatically as incidents cluster "
            "(2+ reports within 500 m in 30 days).")
        return
    if lat is not None and not is_admin(uid):
        items = hotspots_near(lat, lng, hs, within_m=20000)
        title = "🔥 <b>Learned incident hotspots within 20 km</b>"
    else:
        items = [(h, None) for h in hs]
        title = "🔥 <b>All learned incident hotspots</b>"
    if not items:
        await update.effective_message.reply_text("🟢 No learned hotspots within 20 km of you.")
        return
    lines = [title, "<i>Auto-generated from recent incident reports (recency- & severity-weighted).</i>", ""]
    for h, d in items[:10]:
        where = "" if d is None else (" — <b>you're in it</b>" if d <= 0 else f" — {d / 1000:.1f} km away")
        lines.append(f"{RISK_EMOJI[h['risk_level']]} <a href='{maps_link(h['lat'], h['lng'])}'>{h['hotspot_id']}</a> "
                     f"· {h['count']} incidents · mostly {escape(h['dominant_type'])} · score {h['score']}{where}")
    await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def phrases_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        lat, lng, _ = await sh._last_known_location(update.effective_user.id)
        language = "hindi"
        if lat is not None:
            geo = await reverse_geocode(lat, lng)
            language = COUNTRY_LANGUAGE.get((geo or {}).get("country_code") or "", "hindi")
        text = phrasebook_text(language)
        await update.effective_message.reply_html(
            text + "\n\n<i>Other languages:</i> " + ", ".join(PHRASEBOOK)
            + "\n<i>Custom:</i> <code>/phrases assamese where is the police station?</code>")
        return
    language, custom = args[0].lower(), " ".join(args[1:]).strip()
    if not custom and language in PHRASEBOOK:
        await update.effective_message.reply_html(phrasebook_text(language))
        return
    if not custom:
        custom = "Help! Please call the police."
    result = await translate_phrase(custom, language)
    await update.effective_message.reply_html(
        f"🗣️ <b>{escape(language.title())}</b> — “{escape(custom)}”\n\n<b>{escape(result)}</b>\n\n"
        "<i>Show this screen to a local person.</i>")


async def community_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    arg = (context.args or [""])[0].lower()
    if arg in ("on", "off"):
        await update_safety_state(uid, community_alerts=1 if arg == "on" else 0)
        await update.effective_message.reply_text(f"📢 Community safety alerts turned {arg}.")
        return
    s = await get_safety_state(uid)
    await update.effective_message.reply_html(
        f"📢 Community alerts are <b>{'on' if s.get('community_alerts', 1) else 'off'}</b>.\n"
        "When a traveller reports theft, harassment, an accident or a hazard within 2 km of you, you get an "
        "anonymous heads-up (no names, no exact location).\n<code>/community on</code> · <code>/community off</code>")


# ═════════════════════════════════════════════
#  📱 QR Digital ID & 🖥️ web portal
# ═════════════════════════════════════════════

def verification_url(tourist_id: str) -> str:
    return f"{public_base_url()}/verify/{tourist_id}"


def make_qr_png(data: str) -> bytes | None:
    try:
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=10, border=3, dark="#0b1220", light="#ffffff")
    return buf.getvalue()


async def send_id_qr(update: Update, record: dict) -> None:
    url = verification_url(record["tourist_id"])
    png = make_qr_png(url)
    if not png:
        return
    await update.effective_message.reply_photo(
        photo=io.BytesIO(png),
        caption=(f"📱 Scan to verify {record['tourist_id']}\n{url}\n"
                 "Police, hotels and guides can verify your ID against the blockchain ledger instantly."),
    )


async def web_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    base = public_base_url()
    if is_admin(update.effective_user.id):
        await update.effective_message.reply_html(
            "🖥️ <b>Web Command Center</b>\n"
            f"Live map: {base}/command?token={dashboard_token()}\n"
            f"Ledger explorer: {base}/ledger\nID verification: {base}/verify/TID-…\n\n"
            "<i>Keep the command-center link private — it shows live tourist locations.</i>",
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_html(
            f"🌐 <b>Public Safety Portal</b>\n{base}\n• Verify IDs: {base}/verify/TID-…\n• Ledger: {base}/ledger",
            disable_web_page_preview=True,
        )


# ═════════════════════════════════════════════
#  Location hook: proactive hotspot warnings
# ═════════════════════════════════════════════

async def hotspot_location_hook(update: Update, uid: int, lat: float, lng: float) -> None:
    near = hotspots_near(lat, lng, await get_hotspots(), within_m=HOTSPOT_WARN_M)
    if not near:
        return
    state = await get_safety_state(uid)
    try:
        warned = json.loads(state.get("hotspot_alerts") or "{}")
    except ValueError:
        warned = {}
    now = time.time()
    changed = False
    for h, d in near[:2]:
        key = f"{h['lat']:.3f},{h['lng']:.3f}"
        if now - warned.get(key, 0) < HOTSPOT_REWARN_S:
            continue
        warned[key] = now
        changed = True
        where = "You're inside" if d <= 0 else f"~{d:.0f} m ahead is"
        await update.effective_message.reply_html(
            f"🔥 <b>Incident hotspot</b> — {where} an area with <b>{h['count']} recent "
            f"{escape(h['dominant_type'])}</b> reports (learned automatically).\n"
            f"💡 {HOTSPOT_TIPS.get(h['dominant_type'], 'Stay alert and keep to busy areas.')}"
        )
    if changed:
        warned = {k: v for k, v in warned.items() if now - v < 86400}
        await update_safety_state(uid, hotspot_alerts=json.dumps(warned))


# ═════════════════════════════════════════════
#  💸 Free-tier usage (admins)
# ═════════════════════════════════════════════

USAGE_LABELS = {
    "ai": ("🤖 AI (Groq)", "copilot, triage, voice, guide"),
    "weather": ("🌦️ Weather (Open-Meteo)", "alerts, /weather, routes"),
    "routing": ("🧭 Routing (OSRM)", "/saferoute"),
    "places": ("🏥 Places (OpenStreetMap)", "/nearby, SOS"),
}


async def usage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from travel_buddy.core.usage import usage_report
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Only the safety desk can view usage.")
        return
    lines = ["💸 <b>Today's free-tier usage</b> (resets 00:00 UTC)", ""]
    for service, used, limit in usage_report():
        label, what = USAGE_LABELS.get(service, (service, ""))
        pct = int(used * 100 / limit) if limit else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"{label}\n<code>{bar}</code> {used}/{limit} · <i>{what}</i>")
    lines += ["", "Running cost: <b>₹0</b>. When a cap is reached the feature switches to its free offline "
              "mode until tomorrow; safety alerts, SOS, geo-fencing and the ledger never stop."]
    await update.effective_message.reply_html("\n".join(lines))
