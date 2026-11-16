"""
portal_api.py — JSON API behind the tourist & guardian web portal.

Everything a tourist or parent can do in the Telegram bot is available here, using the same
shared logic (travel_buddy.safety.actions) so both channels behave identically:

  Tourist   overview · share location / live tracking · SOS · I'm safe · report incident ·
            safety timer · settings · Digital ID (register / revoke / QR) · guardians ·
            trips (start / end / cancel) · nearby help · safe-route check · E-FIR download
  Guardian  family overview (location, trail, status, incidents) · acknowledge / resolve
  Officer   acknowledge / resolve from the command center
"""

import json
import re
import time
from dataclasses import dataclass, field
from html import escape, unescape

from travel_buddy.core import database
from travel_buddy.core.geocoding import geocode_location
from travel_buddy.core.tracker import (
    _active_trackers, calculate_eta_minutes, haversine_distance_meters, process_location_ping, remove_tracker,
)
from travel_buddy.safety import actions
from travel_buddy.safety.geofence import (
    emergency_numbers_for, find_nearby_services, maps_link, reverse_geocode, zones_near, RISK_ORDER,
)
from travel_buddy.safety.incidents import TYPE_LABEL, admin_ids, is_admin, raise_incident, tourist_label
from travel_buddy.safety.ledger import get_active_tourist_id, issue_tourist_id, revoke_tourist_id, verify_tourist_id
from travel_buddy.safety.monitor import reset_motion
from travel_buddy.safety.risk_intel import assess_route, directions_link, fetch_route, get_hotspots, get_weather_risk
from travel_buddy.safety import store
from travel_buddy.web import auth, runtime

REPORT_TYPES = ["medical", "theft", "harassment", "accident", "lost", "scam", "natural_hazard", "other"]
DOC_TYPES = ["Passport", "Aadhaar", "Driving Licence", "Other ID"]


@dataclass
class Response:
    status: int = 200
    body: object = None                  # dict → JSON; bytes/str → raw
    ctype: str = "application/json"
    headers: list = field(default_factory=list)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def ok(**data) -> Response:
    return Response(200, {"ok": True, **data})


def _num(v, name: str, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"Invalid {name}.")
    if not lo <= f <= hi:
        raise ApiError(400, f"Invalid {name}.")
    return f


# ═════════════════════════════════════════════
#  Read models
# ═════════════════════════════════════════════

def _incident_view(i: dict) -> dict:
    return {
        "id": i["incident_id"], "type": i["incident_type"], "label": TYPE_LABEL.get(i["incident_type"], i["incident_type"]),
        "severity": i["severity"], "status": i["status"], "description": i["description"] or "",
        "lat": i["lat"], "lng": i["lng"], "source": i["source"], "advice": i.get("ai_advice"),
        "created_at": i["created_at"], "resolved_at": i.get("resolved_at"), "block_hash": i.get("block_hash"),
    }


async def _overview(uid: int) -> dict:
    from travel_buddy.bot.intel_handlers import build_live_context
    ctx = await build_live_context(uid)
    state = await store.get_safety_state(uid)
    lat, lng = state["last_lat"], state["last_lng"]
    zones = await store.get_active_zones()
    inside = [z for z in zones if z["zone_id"] in state["zone_state"]["inside"]]
    trip = await database.get_active_or_planning_trip(uid)
    trip_view = None
    if trip:
        trip_view = {k: trip[k] for k in ("trip_id", "destination_name", "destination_lat", "destination_lng",
                                          "transport_mode", "status", "alert_sent")}
        if lat is not None:
            d = haversine_distance_meters(lat, lng, trip["destination_lat"], trip["destination_lng"])
            trip_view["distance_km"] = round(d / 1000, 2)
            trip_view["eta_min"] = round(calculate_eta_minutes(d, None, trip["transport_mode"])[0])
    rec = await get_active_tourist_id(uid)
    guardians = await store.get_guardians(uid)
    hotspots = []
    if lat is not None:
        hotspots = [h for h in await get_hotspots() if h["risk_level"] != "safe"
                    and haversine_distance_meters(lat, lng, h["lat"], h["lng"]) < 30000]
    return {
        "place": ctx.get("place"),
        "score": ctx["_score"],
        "weather": ctx.get("_weather"),
        "local_time": ctx.get("local_time"),
        "location": {"lat": lat, "lng": lng, "seen_ts": state["last_seen_ts"]} if lat is not None else None,
        "zones_inside": [{"name": z["name"], "risk": z["risk_level"], "advisory": z["advisory"]} for z in inside],
        "zones_near": [{"id": z["zone_id"], "name": z["name"], "risk": z["risk_level"], "lat": z["center_lat"],
                        "lng": z["center_lng"], "radius_m": z["radius_m"], "advisory": z["advisory"],
                        "edge_m": round(e)} for z, e in (zones_near(lat, lng, zones, 50000) if lat is not None else [])],
        "hotspots": [{k: h[k] for k in ("hotspot_id", "lat", "lng", "radius_m", "count", "risk_level", "dominant_type")}
                     for h in hotspots],
        "trip": trip_view,
        "trail": [{"lat": p["lat"], "lng": p["lng"], "ts": p["ts"]} for p in await store.get_location_history(uid, 30)],
        "checkin_pending": bool(state["checkin_pending_ts"]),
        "checkin_reason": state["checkin_reason"],
        "timer_due_ts": state["timer_due_ts"],
        "monitoring": bool(state["monitoring_enabled"]),
        "community": bool(state.get("community_alerts", 1)),
        "digital_id": _id_view(rec) if rec else None,
        "guardians": [{"id": g["guardian_row_id"], "name": g["guardian_name"], "linked": bool(g["guardian_telegram_id"])}
                      for g in guardians],
        "incidents": [_incident_view(i) for i in await store.get_user_incidents(uid, 25)],
        "now": time.time(),
    }


def _id_view(rec: dict) -> dict:
    return {k: rec[k] for k in ("tourist_id", "full_name", "nationality", "doc_type", "doc_last4", "emergency_name",
                                "emergency_phone", "valid_from", "valid_until", "block_hash", "status")}


async def _family(uid: int) -> list[dict]:
    from travel_buddy.bot.intel_handlers import build_live_context
    out = []
    for t in (await store.get_tourists_guarded_by(uid))[:10]:
        name, tid, _ = await tourist_label(t)
        state = await store.get_safety_state(t)
        ctx = await build_live_context(t)
        trip = await database.get_active_or_planning_trip(t)
        zones = await store.get_active_zones()
        inside = [z for z in zones if z["zone_id"] in state["zone_state"]["inside"]]
        out.append({
            "id": t, "name": name, "tourist_id": tid, "place": ctx.get("place"), "score": ctx["_score"],
            "location": {"lat": state["last_lat"], "lng": state["last_lng"], "seen_ts": state["last_seen_ts"]}
            if state["last_lat"] is not None else None,
            "zone_risk": max((z["risk_level"] for z in inside), key=lambda r: RISK_ORDER[r], default="safe"),
            "zones_inside": [z["name"] for z in inside],
            "checkin_pending": bool(state["checkin_pending_ts"]),
            "timer_due_ts": state["timer_due_ts"],
            "trip": {"destination": trip["destination_name"], "status": trip["status"], "mode": trip["transport_mode"]}
            if trip else None,
            "trail": [{"lat": p["lat"], "lng": p["lng"], "ts": p["ts"]} for p in await store.get_location_history(t, 20)],
            "incidents": [_incident_view(i) for i in await store.get_user_incidents(t, 10)],
        })
    return out


# ═════════════════════════════════════════════
#  Actions
# ═════════════════════════════════════════════

JOURNEY_TEXT = {
    "TRACKING_STARTED": "🚀 <b>Live journey tracking started</b> — {d:.1f} km to <b>{dest}</b>, ETA ~{eta} min.",
    "APPROACHING_ALERT": "🔔 <b>APPROACHING YOUR STOP!</b> {d:.2f} km to <b>{dest}</b> (~{eta} min). Get ready to get off.",
    "ARRIVED": "🎉 <b>You've arrived at {dest}!</b> Trip completed.",
    "OVERSHOT": "⚠️ <b>Destination passed?</b> You're moving away from <b>{dest}</b> ({d:.2f} km).",
}


async def _web_location(uid: int, lat: float, lng: float) -> list[str]:
    bot = runtime.bot()
    notes: list[str] = []

    async def say(html: str) -> None:
        notes.append(html)

    trip = await database.get_active_or_planning_trip(uid)
    alert_sent = False
    if trip:
        if trip["status"] == "planning":
            await database.activate_trip(trip["trip_id"], uid)
            trip["status"] = "active"
        ev = process_location_ping(trip, lat, lng)
        text = JOURNEY_TEXT.get(ev.event)
        if text:
            msg = text.format(d=ev.distance_km, dest=escape(ev.destination_name), eta=max(1, round(ev.eta_minutes)))
            notes.append(msg)
            if ev.event in ("APPROACHING_ALERT", "ARRIVED"):  # also buzz the phone via Telegram
                try:
                    await bot.send_message(uid, msg, parse_mode="HTML")
                except Exception:
                    pass
        if ev.event == "APPROACHING_ALERT":
            await database.mark_alert_sent(trip["trip_id"])
        if ev.event == "ARRIVED":
            await database.complete_trip(uid)
            remove_tracker(uid)
            reset_motion(uid)
            trip = None
        tracker = _active_trackers.get(uid)
        alert_sent = bool(tracker and tracker.alert_sent)
    await actions.ingest_location(bot, uid, lat, lng, trip, alert_sent, say=say)
    return notes


async def _sos(uid: int) -> dict:
    inc = await actions.trigger_sos(runtime.bot(), uid, source="sos", note="Panic button pressed (web portal).")
    geo = await reverse_geocode(inc["lat"], inc["lng"]) if inc["lat"] is not None else None
    numbers = emergency_numbers_for((geo or {}).get("country_code"))
    services = []
    if inc["lat"] is not None:
        services = [s for s in await find_nearby_services(inc["lat"], inc["lng"], radius_m=5000, per_type=1)
                    if s["type"] in ("police", "hospital")]
    return {"incident": _incident_view(inc), "delivered": inc["delivered"], "fresh": inc["fresh"],
            "emergency_numbers": numbers, "nearby": services}


async def _report(uid: int, body: dict) -> dict:
    from travel_buddy.ai.triage import classify_incident
    kind = body.get("type") if body.get("type") in REPORT_TYPES else "other"
    desc = str(body.get("description") or "").strip()[:1000]
    if len(desc) < 3:
        raise ApiError(400, "Please describe what happened.")
    lat = lng = None
    if body.get("use_location"):
        state = await store.get_safety_state(uid)
        lat, lng = state["last_lat"], state["last_lng"]
    triage = await classify_incident(desc, kind)
    inc = await raise_incident(runtime.bot(), uid, triage["category"], triage["severity"], desc, lat, lng,
                               source="report", ai_advice=triage["advice"])
    return {"incident": _incident_view(inc), "delivered": inc["delivered"], "triage": triage}


async def _register_id(uid: int, b: dict) -> dict:
    if await get_active_tourist_id(uid):
        raise ApiError(409, "You already have an active Digital ID. Revoke it first to issue a new one.")
    name = str(b.get("full_name") or "").strip()
    nationality = str(b.get("nationality") or "").strip()[:40]
    doc_type = b.get("doc_type") if b.get("doc_type") in DOC_TYPES else None
    doc = "".join(str(b.get("doc_number") or "").split())
    e_name = str(b.get("emergency_name") or "").strip()[:60]
    e_phone = str(b.get("emergency_phone") or "").strip()
    if not 2 <= len(name) <= 80:
        raise ApiError(400, "Enter your full name (2–80 characters).")
    if not nationality:
        raise ApiError(400, "Enter your nationality.")
    if not doc_type:
        raise ApiError(400, "Choose a document type.")
    if not re.fullmatch(r"[A-Za-z0-9\-]{4,24}", doc):
        raise ApiError(400, "Document number must be 4–24 letters or digits.")
    if not e_name or not re.fullmatch(r"\+?[\d\s\-()]{6,20}", e_phone):
        raise ApiError(400, "Enter an emergency contact name and a valid phone number.")
    days = int(_num(b.get("days", 7), "number of days", 1, 365))
    rec = await issue_tourist_id(uid, name, nationality, doc_type, doc, e_name, e_phone, days)
    return {"digital_id": _id_view({**rec, "status": "active"})}


async def _start_trip(uid: int, b: dict) -> dict:
    if await database.get_active_or_planning_trip(uid):
        raise ApiError(409, "You already have an ongoing trip. End or cancel it first.")
    dest = str(b.get("destination") or "").strip()[:120]
    mode = "bus" if b.get("mode") == "bus" else "train"
    if len(dest) < 2:
        raise ApiError(400, "Enter a destination.")
    g = await geocode_location(dest)
    if not g:
        raise ApiError(404, f"Couldn't find “{dest}”. Try a more specific station or address.")
    trip_id = await database.create_trip(uid, dest, g[0], g[1], mode)
    return {"trip_id": trip_id, "resolved_as": g[2]}


async def _saferoute(uid: int, b: dict) -> dict:
    state = await store.get_safety_state(uid)
    if state["last_lat"] is None:
        raise ApiError(400, "Share your location first.")
    dest = str(b.get("destination") or "").strip()[:120]
    g = await geocode_location(dest) if dest else None
    if not g:
        raise ApiError(404, "Couldn't find that destination.")
    route = await fetch_route(state["last_lat"], state["last_lng"], g[0], g[1])
    weather = await get_weather_risk(g[0], g[1])
    from travel_buddy.bot.intel_handlers import _local_hour
    res = assess_route(route["path"], await store.get_active_zones(), await get_hotspots(),
                       local_hour=_local_hour(weather, g[1]), weather=weather)
    return {"destination": dest, "resolved_as": g[2], "distance_km": round(route["distance_m"] / 1000, 1),
            "duration_min": round(route["duration_s"] / 60), "source": route["source"], "score": res["score"],
            "verdict": res["verdict"], "reasons": res["reasons"],
            "path": route["path"][:: max(1, len(route["path"]) // 300)],
            "directions": directions_link(state["last_lat"], state["last_lng"], g[0], g[1]),
            "weather": weather["summary"] if weather else None}


async def _nearby(uid: int) -> dict:
    state = await store.get_safety_state(uid)
    lat, lng = state["last_lat"], state["last_lng"]
    if lat is None:
        raise ApiError(400, "Share your location first.")
    services = await find_nearby_services(lat, lng)
    geo = await reverse_geocode(lat, lng)
    return {"services": [{**s, "map": maps_link(s["lat"], s["lng"])} for s in services],
            "emergency_numbers": emergency_numbers_for((geo or {}).get("country_code")),
            "country": (geo or {}).get("country_code")}


async def _incident_action(inc_id: int, action: str, uid: int | None, desk: bool, actor_name: str) -> dict:
    inc = await store.get_incident(inc_id)
    if not inc:
        raise ApiError(404, "Incident not found.")
    if not desk and not await actions.can_manage(uid, inc):
        raise ApiError(403, "You don't have access to this incident.")
    acting = uid if uid is not None else next(iter(admin_ids()), 0)
    if action == "ack":
        await actions.acknowledge_as(runtime.bot(), inc, acting, actor_name)
    else:
        await actions.resolve_as(runtime.bot(), inc, acting, role="admin" if desk else None)
    return {"incident": _incident_view(await store.get_incident(inc_id))}


# ═════════════════════════════════════════════
#  Assistant: copilot, safety briefing, phrasebook, travel guide
# ═════════════════════════════════════════════

ALLOWED_TAGS = ("b", "i", "u", "s", "code")


def safe_html(text: str) -> str:
    """AI output → safe HTML: escape everything, then re-allow a few formatting tags only."""
    out = escape(unescape(text or ""))  # decode entities first so they aren't shown double-escaped
    for tag in ALLOWED_TAGS:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return out


async def _ask(uid: int, question: str) -> dict:
    from travel_buddy.ai.copilot import answer_copilot, detect_distress
    from travel_buddy.ai.triage import keyword_triage
    from travel_buddy.bot.intel_handlers import build_live_context
    question = question.strip()[:800]
    if not question:
        raise ApiError(400, "Type a question.")
    if detect_distress(question) or keyword_triage(question)["severity"] == "critical":
        return {"distress": True, "html": "🚨 <b>This sounds like an emergency.</b> Hold the SOS button to alert your "
                                          "guardians and the safety desk with your location — or call <b>112</b> now."}
    ctx = {k: v for k, v in (await build_live_context(uid)).items() if not k.startswith("_")}
    return {"distress": False, "html": safe_html(await answer_copilot(question, ctx, channel="web"))}


async def _default_place(uid: int) -> str | None:
    trip = await database.get_active_or_planning_trip(uid)
    if trip:
        return trip["destination_name"]
    state = await store.get_safety_state(uid)
    if state["last_lat"] is not None:
        geo = await reverse_geocode(state["last_lat"], state["last_lng"])
        if geo:
            parts = [p.strip() for p in geo["display_name"].split(",")]
            return ", ".join(parts[1:3]) if len(parts) > 2 else geo["display_name"]
    return None


async def _safetytips(uid: int, place: str) -> dict:
    from travel_buddy.ai.triage import generate_safety_briefing
    place = place.strip()[:80] or (await _default_place(uid) or "")
    if not place:
        raise ApiError(400, "Enter a place, or share your location / start a trip first.")
    return {"place": place, "html": safe_html(await generate_safety_briefing(place))}


async def _phrases(uid: int) -> dict:
    from travel_buddy.ai.copilot import COUNTRY_LANGUAGE, PHRASE_KEYS, PHRASEBOOK
    auto = "hindi"
    state = await store.get_safety_state(uid)
    if state["last_lat"] is not None:
        geo = await reverse_geocode(state["last_lat"], state["last_lng"])
        auto = COUNTRY_LANGUAGE.get((geo or {}).get("country_code") or "", "hindi")
    return {"auto": auto, "keys": PHRASE_KEYS, "phrases": PHRASEBOOK}


async def _translate(body: dict) -> dict:
    from travel_buddy.ai.copilot import translate_phrase
    lang = str(body.get("language") or "").strip()[:30]
    text = str(body.get("text") or "").strip()[:200]
    if not lang or not text:
        raise ApiError(400, "Enter a language and a phrase.")
    return {"language": lang, "text": text, "result": re.sub(r"<[^>]+>", "", await translate_phrase(text, lang))}


PREF_BUDGETS = ("low", "medium", "high")
PREF_PACES = ("relaxed", "packed")


async def _save_preferences(uid: int, body: dict) -> dict:
    interests = str(body.get("interests") or "").strip()[:120]
    budget = body.get("budget_level") if body.get("budget_level") in PREF_BUDGETS else None
    pace = body.get("pace") if body.get("pace") in PREF_PACES else None
    if not interests or not budget or not pace:
        raise ApiError(400, "Choose your interests, budget and pace.")
    await database.save_user_preferences(uid, interests, budget, pace)
    return {"preferences": await database.get_user_preferences(uid)}


async def _explore(uid: int, query: str) -> dict:
    from travel_buddy.ai.travel_guide import generate_travel_guide
    query = query.strip()[:300]
    if not query:
        raise ApiError(400, "Ask something, e.g. “best local food nearby”.")
    prefs = await database.get_user_preferences(uid)
    if not prefs:
        raise ApiError(409, "Set your travel preferences first.")
    trip = await database.get_active_or_planning_trip(uid)
    dest = trip["destination_name"] if trip else None
    return {"context": dest, "html": safe_html(await generate_travel_guide(query, dest, prefs))}


# ═════════════════════════════════════════════
#  Photo evidence
# ═════════════════════════════════════════════

MAX_EVIDENCE_BYTES = 6 * 1024 * 1024
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")


async def _evidence(uid: int, body: dict) -> dict:
    import base64
    raw = str(body.get("image_b64") or "")
    if "," in raw[:100]:
        raw = raw.split(",", 1)[1]  # strip a data: URL prefix
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        raise ApiError(400, "That image couldn't be read.")
    if not data or len(data) > MAX_EVIDENCE_BYTES or not data.startswith(IMAGE_MAGIC):
        raise ApiError(400, "Please upload a JPEG, PNG or WebP photo under 6 MB.")
    incident = None
    if body.get("incident_id"):
        incident = await store.get_incident(int(_num(body["incident_id"], "incident", 1, 10**9)))
        if not incident or incident["telegram_id"] != uid:
            raise ApiError(404, "Incident not found.")
    try:
        res = await actions.add_photo_evidence(runtime.bot(), uid, data, str(body.get("caption") or ""), incident=incident)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return {"incident": _incident_view(await store.get_incident(res["incident"]["incident_id"])),
            "created": res["created"], "sha256": res["sha256"], "block_index": res["block_index"],
            "forwarded": res["forwarded"]}


# ═════════════════════════════════════════════
#  Safety desk (officers)
# ═════════════════════════════════════════════

ZONE_RISKS = ("safe", "caution", "high", "restricted")


def _zone_view(z: dict) -> dict:
    return {"id": z["zone_id"], "name": z["name"], "risk": z["risk_level"], "lat": z["center_lat"],
            "lng": z["center_lng"], "radius_m": z["radius_m"], "advisory": z["advisory"] or "",
            "created_at": z["created_at"]}


async def _add_zone(body: dict, by_uid: int | None) -> dict:
    risk = body.get("risk") if body.get("risk") in ZONE_RISKS else None
    name = str(body.get("name") or "").strip()[:80]
    if not risk or len(name) < 2:
        raise ApiError(400, "Give the zone a name and a risk level.")
    lat = _num(body.get("lat"), "latitude", -90, 90)
    lng = _num(body.get("lng"), "longitude", -180, 180)
    radius = _num(body.get("radius_m"), "radius", 50, 200000)
    zid = await store.add_zone(name, lat, lng, radius, risk, str(body.get("advisory") or "").strip()[:300], by_uid)
    return {"zone": _zone_view(next(z for z in await store.get_active_zones() if z["zone_id"] == zid))}


# ═════════════════════════════════════════════
#  Router
# ═════════════════════════════════════════════

def handle(method: str, path: str, body: dict, session: dict | None, desk_token_ok: bool,
           client: str) -> Response:
    try:
        return _route(method, path, body, session, desk_token_ok, client)
    except ApiError as exc:
        return Response(exc.status, {"ok": False, "error": exc.message})


def _route(method, path, body, session, desk_token_ok, client) -> Response:
    # ── Authentication ──
    if path == "/api/auth/request" and method == "POST":
        if auth.rate_limited(f"req:{client}"):
            raise ApiError(429, "Too many attempts. Please wait a few minutes.")
        uid = auth.find_user(str(body.get("identifier", "")))
        if uid:
            code = auth.issue_code(uid)
            if code:
                runtime.run(runtime.bot().send_message(
                    uid, f"🔐 <b>Travel Buddy web login code:</b> <code>{code}</code>\n\nValid for 10 minutes. "
                         "If you didn't try to sign in, ignore this message.", parse_mode="HTML"), timeout=20)
        # Same answer either way, so the form can't be used to discover who uses the bot.
        return ok(message="If that account uses Travel Buddy, a 6-digit code was sent to its Telegram chat.")

    if path == "/api/auth/verify" and method == "POST":
        if auth.rate_limited(f"ver:{client}", limit=20):
            raise ApiError(429, "Too many attempts. Please wait a few minutes.")
        uid = auth.find_user(str(body.get("identifier", "")))
        if not uid or not auth.verify_code(uid, str(body.get("code", ""))):
            raise ApiError(401, "That code is wrong or has expired (codes last 10 minutes). Tap “Send a new code”.")
        token = auth.create_session(uid)
        return Response(200, {"ok": True}, headers=[("Set-Cookie", session_cookie(token))])

    if path == "/api/auth/logout" and method == "POST":
        return Response(200, {"ok": True}, headers=[("Set-Cookie", session_cookie("", clear=True))])

    # ── Officer / guardian incident actions ──
    m = re.fullmatch(r"/api/incident/(\d+)/(ack|resolve)", path)
    if m and method == "POST":
        if desk_token_ok and not session:
            pass
        elif session and body.get("_csrf_ok") is not True and not desk_token_ok:
            raise ApiError(403, "Security check failed. Reload the page and try again.")
        if session and not desk_token_ok:
            name = runtime.run(tourist_label(session["uid"]))[0]
            data = runtime.run(_incident_action(int(m.group(1)), m.group(2), session["uid"],
                                                is_admin(session["uid"]), name))
        elif desk_token_ok:
            data = runtime.run(_incident_action(int(m.group(1)), m.group(2), None, True, "The safety desk"))
        else:
            raise ApiError(401, "Please sign in.")
        return ok(**data)

    # ── Safety desk: zone management & free-tier usage (admin sign-in or desk token) ──
    if path.startswith("/api/desk/"):
        if not (desk_token_ok or (session and is_admin(session["uid"]))):
            raise ApiError(403, "Safety desk only.")
        if method == "POST" and not desk_token_ok and body.get("_csrf_ok") is not True:
            raise ApiError(403, "Security check failed. Reload the page and try again.")
        by_uid = session["uid"] if session else next(iter(admin_ids()), None)
        if path == "/api/desk/zones" and method == "GET":
            return ok(zones=[_zone_view(z) for z in runtime.run(store.get_active_zones())])
        if path == "/api/desk/zones" and method == "POST":
            return ok(**runtime.run(_add_zone(body, by_uid)))
        m = re.fullmatch(r"/api/desk/zones/(\d+)/remove", path)
        if m and method == "POST":
            runtime.run(store.deactivate_zone(int(m.group(1))))
            return ok()
        if path == "/api/desk/usage" and method == "GET":
            from travel_buddy.core.usage import usage_report
            return ok(usage=[{"service": s, "used": u, "limit": lim} for s, u, lim in usage_report()])
        raise ApiError(404, "Not found.")

    # ── Everything below needs a signed-in user ──
    if not session:
        raise ApiError(401, "Please sign in.")
    uid = session["uid"]

    m = re.fullmatch(r"/api/incident/(\d+)/efir", path)
    if m and method == "GET":
        inc = runtime.run(store.get_incident(int(m.group(1))))
        if not inc or not runtime.run(actions.can_manage(uid, inc)):
            raise ApiError(404, "Incident not found.")
        text = runtime.run(actions.build_efir_text(inc))
        return Response(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                        [("Content-Disposition", f'attachment; filename="EFIR_draft_incident_{inc["incident_id"]}.txt"')])

    if method == "GET":
        if path == "/api/me":
            user = runtime.run(database.get_user(uid)) or {}
            name, tid, _ = runtime.run(tourist_label(uid))
            return ok(uid=uid, name=name, username=user.get("username"), tourist_id=tid, csrf=session["csrf"],
                      is_admin=is_admin(uid), guarding=len(runtime.run(store.get_tourists_guarded_by(uid))),
                      bot_username=runtime.bot_username())
        if path == "/api/me/overview":
            return ok(**runtime.run(_overview(uid)))
        if path == "/api/family":
            return ok(people=runtime.run(_family(uid)))
        if path == "/api/me/nearby":
            return ok(**runtime.run(_nearby(uid)))
        if path == "/api/me/phrases":
            return ok(**runtime.run(_phrases(uid)))
        if path == "/api/me/preferences":
            return ok(preferences=runtime.run(database.get_user_preferences(uid)))
        if path == "/api/me/id/qr.png":
            from travel_buddy.bot.intel_handlers import make_qr_png, verification_url
            rec = runtime.run(get_active_tourist_id(uid))
            png = make_qr_png(verification_url(rec["tourist_id"])) if rec else None
            if not png:
                raise ApiError(404, "No Digital ID.")
            return Response(200, png, "image/png")
        raise ApiError(404, "Not found.")

    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    if body.get("_csrf_ok") is not True:
        raise ApiError(403, "Security check failed. Reload the page and try again.")

    if path == "/api/me/location":
        lat, lng = _num(body.get("lat"), "latitude", -90, 90), _num(body.get("lng"), "longitude", -180, 180)
        return ok(notes=runtime.run(_web_location(uid, lat, lng)))
    if path == "/api/me/sos":
        if body.get("lat") is not None:
            runtime.run(_web_location(uid, _num(body["lat"], "latitude", -90, 90),
                                      _num(body["lng"], "longitude", -180, 180)))
        return ok(**runtime.run(_sos(uid)))
    if path == "/api/me/safe":
        res = runtime.run(actions.mark_safe(runtime.bot(), uid))
        return ok(**res, message=re.sub(r"<[^>]+>", "", actions.mark_safe_text(res)))
    if path == "/api/me/report":
        return ok(**runtime.run(_report(uid, body)))
    if path == "/api/me/timer":
        minutes = int(_num(body.get("minutes", 0), "minutes", 0, 720))
        if 0 < minutes < 5:
            raise ApiError(400, "Timer must be between 5 and 720 minutes.")
        return ok(due_ts=runtime.run(actions.set_timer(uid, minutes)))
    if path == "/api/me/settings":
        fields = {}
        if "monitoring" in body:
            fields["monitoring_enabled"] = 1 if body["monitoring"] else 0
            if not body["monitoring"]:
                reset_motion(uid)
        if "community" in body:
            fields["community_alerts"] = 1 if body["community"] else 0
        if fields:
            runtime.run(store.update_safety_state(uid, **fields))
        return ok()
    if path == "/api/me/id":
        return ok(**runtime.run(_register_id(uid, body)))
    if path == "/api/me/id/revoke":
        rec = runtime.run(get_active_tourist_id(uid))
        if not rec:
            raise ApiError(404, "No active Digital ID.")
        runtime.run(revoke_tourist_id(rec["tourist_id"], "user_request_web"))
        return ok()
    if path == "/api/me/id/verify":
        rec = runtime.run(get_active_tourist_id(uid))
        if not rec:
            raise ApiError(404, "No active Digital ID.")
        res = runtime.run(verify_tourist_id(rec["tourist_id"]))
        return ok(valid=res["valid"], reasons=res["reasons"])
    if path == "/api/me/guardians":
        link = runtime.run(actions.new_guardian_invite(uid, str(body.get("name") or "Guardian").strip(),
                                                       runtime.bot_username()))
        return ok(link=link)
    m = re.fullmatch(r"/api/me/guardians/(\d+)/remove", path)
    if m:
        if not runtime.run(store.remove_guardian(uid, int(m.group(1)))):
            raise ApiError(404, "Guardian not found.")
        return ok()
    if path == "/api/me/trip":
        return ok(**runtime.run(_start_trip(uid, body)))
    if path in ("/api/me/trip/end", "/api/me/trip/cancel"):
        remove_tracker(uid)
        reset_motion(uid)
        done = runtime.run(database.complete_trip(uid) if path.endswith("end") else database.cancel_trip(uid))
        if not done:
            raise ApiError(404, "No ongoing trip.")
        return ok()
    if path == "/api/me/saferoute":
        return ok(**runtime.run(_saferoute(uid, body)))
    if path == "/api/me/ask":
        return ok(**runtime.run(_ask(uid, str(body.get("question") or "")), timeout=90))
    if path == "/api/me/safetytips":
        return ok(**runtime.run(_safetytips(uid, str(body.get("place") or "")), timeout=90))
    if path == "/api/me/phrases/translate":
        return ok(**runtime.run(_translate(body)))
    if path == "/api/me/preferences":
        return ok(**runtime.run(_save_preferences(uid, body)))
    if path == "/api/me/explore":
        return ok(**runtime.run(_explore(uid, str(body.get("query") or "")), timeout=90))
    if path == "/api/me/evidence":
        return ok(**runtime.run(_evidence(uid, body)))
    raise ApiError(404, "Not found.")


def session_cookie(token: str, clear: bool = False) -> str:
    from travel_buddy.web.dashboard import public_base_url
    secure = "; Secure" if public_base_url().startswith("https://") else ""
    if clear:
        return f"{auth.COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
    return f"{auth.COOKIE}={token}; Path=/; Max-Age={auth.SESSION_TTL_S}; HttpOnly; SameSite=Lax{secure}"


def parse_json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "Invalid JSON.")
    if not isinstance(data, dict):
        raise ApiError(400, "Invalid JSON.")
    return data
