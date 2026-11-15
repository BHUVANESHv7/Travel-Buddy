"""
handlers.py — All Telegram command and message handlers for Travel Buddy Bot.
Includes:
- /start, /help
- /newtrip (ConversationHandler: name -> transport mode -> location/geocoding)
- /endtrip, /canceltrip, /mytrip
- Live location tracking with Haversine distance, speed/ETA smoothing, and proximity alerts
"""

import logging
from telegram import (
    Update,
    ReplyKeyboardMarkup,

)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from database import (
    upsert_user,
    get_active_or_planning_trip,
    create_trip,
    activate_trip,
    complete_trip,
    cancel_trip,
    mark_alert_sent,
    get_user_preferences,
    save_user_preferences,
)
from geocoding import geocode_location
from tracker import (
    process_location_ping,
    remove_tracker,
    _active_trackers,
    calculate_eta_minutes,
    haversine_distance_meters,
)
from ai_guide import generate_travel_guide
from keyboards import MAIN_KEYBOARD
from safety_handlers import (
    handle_guardian_deeplink,
    safety_location_hook,
    location_snapshot_reply,
)
from safety_monitor import reset_motion

logger = logging.getLogger(__name__)

# Conversation states for /newtrip flow
DESTINATION_NAME, TRANSPORT_MODE, DESTINATION_LOCATION = range(3)

# Conversation states for /preferences onboarding flow (Phase 4)
PREF_INTERESTS, PREF_BUDGET, PREF_PACE = range(10, 13)


# ─────────────────────────────────────────────
#  /start & /help
# ─────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /start command.
    Greets the user and registers them in the database.
    """
    user = update.effective_user
    if user is None:
        return

    telegram_id = user.id
    username = user.username or user.first_name or "Traveller"

    is_new = await upsert_user(telegram_id, username)

    # Guardian invite deep link: t.me/<bot>?start=g_<code>
    if context.args and context.args[0].startswith("g_"):
        await handle_guardian_deeplink(update, context, context.args[0][2:])
        return
    logger.info("/start called by user %s (new=%s)", telegram_id, is_new)

    greeting = "Welcome" if is_new else "Welcome back"

    welcome_text = (
        f"👋 {greeting}, <b>{user.first_name}</b>!\n\n"
        "🚆 <b>Travel Buddy</b> is your smart journey companion.\n\n"
        "I track your live location during train/bus journeys and alert you "
        "before your stop arrives, and I can also suggest places to visit.\n\n"
        "<b>Getting Started:</b>\n"
        "1️⃣ Plan a journey with /newtrip\n"
        "2️⃣ Share your <b>Live Location</b> (📎 ➔ Location ➔ Share Live Location)\n"
        "3️⃣ Relax! I'll alert you 3-5 minutes before your stop arrives.\n\n"
        "🛡️ <b>Stay safe while you travel:</b>\n"
        "• /registerid — blockchain-secured Digital Tourist ID\n"
        "• /addguardian — link family who get your alerts\n"
        "• 🆘 <b>SOS</b> button below — instant alert with your location\n"
        "• Automatic geo-fence warnings & AI anomaly detection on trips\n"
        "• 🤖 Just <b>type or send a voice note</b> — your AI safety copilot answers\n\n"
        "Type /help to see all available commands."
    )

    await update.message.reply_html(welcome_text, reply_markup=MAIN_KEYBOARD)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /help command.
    Lists all available commands and explains live location tracking.
    """
    help_text = (
        "📋 <b>Travel Buddy — Command Reference</b>\n\n"
        "─────────────────────────\n"
        "🚀 <b>Trip Management</b>\n"
        "/newtrip    — Plan a new journey (sets destination & mode)\n"
        "/mytrip     — View current trip, distance remaining & ETA\n"
        "/endtrip    — Mark current trip as completed\n"
        "/canceltrip — Cancel current trip or abort trip planning\n\n"
        "🤖 <b>AI Travel Guide (Groq + Web Search)</b>\n"
        "/explore    — Ask personalized recommendations (e.g. <code>/explore top food spots</code>)\n"
        "/preferences — Set or update your travel interests, budget & pace\n\n"
        "📍 <b>Live Tracking & Alerts</b>\n"
        "• While on your train/bus, share your <b>Live Location</b>:\n"
        "  Tap 📎 (attachment) ➔ <b>Location</b> ➔ <b>Share Live Location for…</b>\n"
        "• Your trip automatically activates from 🟡 <i>planning</i> ➔ 🟢 <i>active</i>\n"
        "• You'll get an alert when you are within <b>~3-5 minutes</b> or <b>1 km</b> of your stop!\n"
        "• Reaching your stop (within ~150m) auto-completes the trip.\n\n"
        "🛡️ <b>Tourist Safety</b>\n"
        "/sos — 🆘 Panic alert to guardians & safety desk with your location\n"
        "/report — Report theft, harassment, medical, accident… (AI triage)\n"
        "/safety — Your live safety score & status\n"
        "/nearby — Closest police, hospitals & emergency numbers\n"
        "/zones — Risk zones (geo-fences) around you\n"
        "/safetytips [place] — AI safety briefing (scams, risky areas)\n"
        "/timer 60 — Alert guardians if you don't check in within 60 min\n"
        "/safe — I'm okay (answers check-ins, cancels timer)\n"
        "/incidents · /resolve ID · /efir ID — Manage incidents & draft a police report\n"
        "/monitoring on|off — Auto anomaly detection during trips\n\n"
        "🪪 <b>Digital Tourist ID (Blockchain)</b>\n"
        "/registerid — Get a tamper-proof Digital Tourist ID\n"
        "/myid · /verifyid TID · /revokeid · /ledger\n\n"
        "👪 <b>Guardians</b>\n"
        "/addguardian Name — Invite an emergency contact\n"
        "/guardians · /removeguardian ID · /whereis\n\n"
        "🚀 <b>Next-gen Intelligence</b>\n"
        "/ask — 🤖 AI copilot that knows your live situation (or just type)\n"
        "🎙️ Send a <b>voice note</b> in any language — distress words trigger SOS\n"
        "📸 Send a <b>photo</b> with a caption — sealed as tamper-proof evidence\n"
        "/saferoute place — Route safety vs zones, hotspots, weather & night\n"
        "/weather · /hotspots — Live hazards & self-learned incident hotspots\n"
        "/phrases [language] [text] — Emergency phrases for locals\n"
        "/community on|off — Anonymous alerts about incidents near you\n"
        "/web — Public verification portal (admins: live command-center map)\n\n"
        "🖥️ <b>Safety desk (admins)</b>\n"
        "/dashboard · /incidents all · /addzone · /removezone · /whereis TID\n\n"
        "ℹ️ <b>General</b>\n"
        "/start      — Start bot & register account\n"
        "/help       — Show this command guide\n\n"
        "─────────────────────────\n"
        "💡 <i>Tip: If you have an active trip, /explore automatically uses your destination as context!</i>"
    )
    await update.message.reply_html(help_text)


# ─────────────────────────────────────────────
#  /newtrip Conversation Flow
# ─────────────────────────────────────────────

async def new_trip_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /newtrip.
    Checks for ongoing trips. If none, prompts for destination name.
    """
    user = update.effective_user
    if user is None:
        return ConversationHandler.END

    await upsert_user(user.id, user.username or user.first_name)

    ongoing = await get_active_or_planning_trip(user.id)
    if ongoing:
        await update.message.reply_html(
            f"⚠️ You already have an ongoing trip to <b>{ongoing['destination_name']}</b>.\n"
            "Finish or cancel it first using /endtrip or /canceltrip."
        )
        return ConversationHandler.END

    context.user_data.clear()

    await update.message.reply_html(
        "🗺️ <b>Let's plan your new trip!</b>\n\n"
        "<b>Step 1/3:</b> What is your destination's name?\n"
        "<i>(e.g., Grand Central Terminal, Berlin Hauptbahnhof, Kyoto Station)</i>\n\n"
        "💡 <i>Send /canceltrip at any time to cancel.</i>"
    )
    return DESTINATION_NAME


async def destination_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives destination name and prompts for transport mode.
    """
    dest_name = update.message.text.strip()
    if not dest_name:
        await update.message.reply_text("Please enter a valid destination name.")
        return DESTINATION_NAME

    context.user_data["destination_name"] = dest_name

    reply_keyboard = [["🚆 Train", "🚌 Bus"]]
    await update.message.reply_html(
        f"Destination: <b>{dest_name}</b>\n\n"
        "<b>Step 2/3:</b> How will you be traveling? Please choose your transport mode:",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return TRANSPORT_MODE


async def transport_mode_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives transport mode ('train' or 'bus') and prompts for location/coordinates.
    """
    text = update.message.text.strip().lower()
    if "train" in text:
        mode = "train"
    elif "bus" in text:
        mode = "bus"
    else:
        await update.message.reply_html(
            "⚠️ Please select either <b>Train</b> or <b>Bus</b> using the buttons below:",
            reply_markup=ReplyKeyboardMarkup(
                [["🚆 Train", "🚌 Bus"]], one_time_keyboard=True, resize_keyboard=True
            ),
        )
        return TRANSPORT_MODE

    context.user_data["transport_mode"] = mode

    await update.message.reply_html(
        f"Transport Mode: <b>{'🚆 Train' if mode == 'train' else '🚌 Bus'}</b>\n\n"
        "<b>Step 3/3:</b> Now share the destination's location:\n\n"
        "You can use either option:\n"
        "• 📍 <b>Send Location:</b> Tap 📎 (attachment) ➔ <b>Location</b> to drop a pin.\n"
        "• ✍️ <b>Type Address:</b> Type the station, city, or address, and I will geocode it automatically!\n\n"
        "<i>Send /canceltrip to abort.</i>",
        reply_markup=MAIN_KEYBOARD,
    )
    return DESTINATION_LOCATION


async def destination_location_pin_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Handles destination sent directly as a Telegram Location object.
    """
    user = update.effective_user
    location = update.message.location
    if not location:
        await update.message.reply_text("Please send a valid location or type an address.")
        return DESTINATION_LOCATION

    lat = location.latitude
    lng = location.longitude
    dest_name = context.user_data.get("destination_name", "Destination")
    mode = context.user_data.get("transport_mode", "train")

    trip_id = await create_trip(
        user_id=user.id,
        destination_name=dest_name,
        destination_lat=lat,
        destination_lng=lng,
        transport_mode=mode,
    )
    context.user_data.clear()

    await update.message.reply_html(
        f"🎉 <b>Trip Created Successfully!</b>\n\n"
        f"• <b>Trip ID:</b> #{trip_id}\n"
        f"• <b>Destination:</b> <b>{dest_name}</b>\n"
        f"• <b>Transport:</b> {'🚆 Train' if mode == 'train' else '🚌 Bus'}\n"
        f"• <b>Coordinates:</b> <code>{lat:.5f}, {lng:.5f}</code>\n"
        f"• <b>Status:</b> 🟡 <code>planning</code>\n\n"
        "🚀 <b>Next Step:</b> When you board, tap 📎 ➔ <b>Location</b> ➔ <b>Share Live Location</b>.\n"
        "I'll track your speed and alert you before your stop!"
    )
    return ConversationHandler.END


async def destination_text_geocoded(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Handles destination sent as free text, geocoding it via OpenStreetMap Nominatim.
    """
    user = update.effective_user
    query = update.message.text.strip()

    status_msg = await update.message.reply_text(f"🔍 Searching coordinates for '{query}'…")

    geocode_result = await geocode_location(query)
    if not geocode_result:
        await status_msg.edit_text(
            f"❌ Could not find coordinates for '{query}'.\n\n"
            "Please try typing a more specific address or station name, "
            "or send a location pin via the 📎 attachment menu.\n\n"
            "(Send /canceltrip to abort)"
        )
        return DESTINATION_LOCATION

    lat, lng, display_name = geocode_result
    dest_name = context.user_data.get("destination_name", query)
    mode = context.user_data.get("transport_mode", "train")

    trip_id = await create_trip(
        user_id=user.id,
        destination_name=dest_name,
        destination_lat=lat,
        destination_lng=lng,
        transport_mode=mode,
    )
    context.user_data.clear()

    await status_msg.delete()
    await update.message.reply_html(
        f"🎉 <b>Trip Created Successfully!</b>\n\n"
        f"• <b>Trip ID:</b> #{trip_id}\n"
        f"• <b>Destination:</b> <b>{dest_name}</b>\n"
        f"• <b>Transport:</b> {'🚆 Train' if mode == 'train' else '🚌 Bus'}\n"
        f"• <b>Coordinates:</b> <code>{lat:.5f}, {lng:.5f}</code>\n"
        f"• <b>Resolved As:</b> <i>{display_name[:120]}…</i>\n"
        f"• <b>Status:</b> 🟡 <code>planning</code>\n\n"
        "🚀 <b>Next Step:</b> When you board, tap 📎 ➔ <b>Location</b> ➔ <b>Share Live Location</b>.\n"
        "I will track your transit and alert you 3-5 minutes before your stop!"
    )
    return ConversationHandler.END


async def cancel_trip_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Aborts the /newtrip conversation flow without creating a trip.
    """
    context.user_data.clear()
    await update.message.reply_html(
        "🚫 <b>Trip planning aborted.</b> No new trip was created.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  /mytrip, /endtrip, /canceltrip
# ─────────────────────────────────────────────

async def my_trip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /mytrip command.
    Displays current trip details, coordinates, remaining distance, speed, and ETA.
    """
    user = update.effective_user
    if user is None:
        return

    trip = await get_active_or_planning_trip(user.id)
    if not trip:
        await update.message.reply_html(
            "ℹ️ <b>No active trip.</b>\n\n"
            "You don't have any ongoing or planned journeys. "
            "Start one with /newtrip!"
        )
        return

    status = trip["status"]
    status_emoji = "🟡" if status == "planning" else "🟢"
    mode_emoji = "🚆" if trip["transport_mode"] == "train" else "🚌"
    created = trip["created_at"][:19].replace("T", " ")

    # Check if we have live tracking metrics in memory
    live_info = ""
    tracker_state = _active_trackers.get(user.id)
    if tracker_state and len(tracker_state.pings) > 0:
        latest = tracker_state.pings[-1]
        dist_m = haversine_distance_meters(
            latest.lat, latest.lng, tracker_state.destination_lat, tracker_state.destination_lng
        )
        dist_km = dist_m / 1000.0
        eta_min, _ = calculate_eta_minutes(dist_m, None, tracker_state.transport_mode)

        live_info = (
            f"\n📊 <b>Live Tracking Status:</b>\n"
            f"• <b>Remaining Distance:</b> <code>{dist_km:.2f} km</code> ({dist_m:.0f} m)\n"
            f"• <b>Estimated ETA:</b> ~<code>{int(round(eta_min))} mins</code>\n"
            f"• <b>Alert Sent:</b> {'✅ Yes' if tracker_state.alert_sent else '⏳ Pending'}\n"
        )
    elif status == "planning":
        live_info = (
            "\n💡 <i>Share your Live Location (📎 ➔ Location ➔ Share Live Location) "
            "to activate GPS journey tracking and alerts!</i>\n"
        )

    trip_text = (
        "📍 <b>Current Trip Details</b>\n\n"
        f"• <b>Trip ID:</b> #{trip['trip_id']}\n"
        f"• <b>Destination:</b> <b>{trip['destination_name']}</b>\n"
        f"• <b>Transport Mode:</b> {mode_emoji} {trip['transport_mode'].capitalize()}\n"
        f"• <b>Destination GPS:</b> <code>{trip['destination_lat']:.5f}, {trip['destination_lng']:.5f}</code>\n"
        f"• <b>Status:</b> {status_emoji} <b>{status.capitalize()}</b>\n"
        f"• <b>Planned:</b> {created} UTC\n"
        f"{live_info}\n"
        "<b>Actions:</b>\n"
        "• /endtrip — Conclude and complete trip\n"
        "• /canceltrip — Cancel this trip"
    )
    await update.message.reply_html(trip_text)


async def end_trip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /endtrip command.
    Marks active/planning trip as 'completed', clears live tracker, and resets user status.
    """
    user = update.effective_user
    if user is None:
        return

    remove_tracker(user.id)
    reset_motion(user.id)
    completed = await complete_trip(user.id)
    if not completed:
        await update.message.reply_html(
            "ℹ️ You don't have an ongoing trip to end. Start a new one with /newtrip!"
        )
        return

    completed_time = completed["completed_at"][:19].replace("T", " ")
    await update.message.reply_html(
        f"🏁 <b>Trip #{completed['trip_id']} Completed!</b>\n\n"
        f"• <b>Destination:</b> {completed['destination_name']}\n"
        f"• <b>Status:</b> ✅ <code>completed</code>\n"
        f"• <b>Completed At:</b> {completed_time} UTC\n\n"
        "Your trip status has been reset to <code>no_active_trip</code>.\n"
        "Hope you had a safe and pleasant journey! 🚆"
    )


async def cancel_trip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle standalone /canceltrip command.
    Marks active/planning trip as 'cancelled', clears live tracker, and resets user status.
    """
    user = update.effective_user
    if user is None:
        return

    remove_tracker(user.id)
    reset_motion(user.id)
    cancelled = await cancel_trip(user.id)
    if not cancelled:
        await update.message.reply_html(
            "ℹ️ You don't have an ongoing trip to cancel."
        )
        return

    cancelled_time = cancelled["completed_at"][:19].replace("T", " ")
    await update.message.reply_html(
        f"❌ <b>Trip #{cancelled['trip_id']} Cancelled.</b>\n\n"
        f"• <b>Destination:</b> {cancelled['destination_name']}\n"
        f"• <b>Status:</b> 🚫 <code>cancelled</code>\n"
        f"• <b>Cancelled At:</b> {cancelled_time} UTC\n\n"
        "Your trip status has been reset to <code>no_active_trip</code>."
    )


# ─────────────────────────────────────────────
#  Live Location Tracking & Proximity Alerting
# ─────────────────────────────────────────────

async def live_location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Processes incoming location updates (both one-off pins and continuous live streams).
    Controls:
      - planning -> active status transitions
      - Speed & ETA calculation
      - Proximity alert trigger (ETA <= 4 min OR dist <= 1km)
      - Auto-completion on arrival (dist <= 150m)
      - Overshoot warnings
    """
    user = update.effective_user
    msg = update.effective_message

    if not user or not msg or not msg.location:
        return

    location = msg.location
    lat = location.latitude
    lng = location.longitude

    # Check if this update comes from an edited message (standard for Telegram live location pings)
    is_live_stream = (update.edited_message is not None) or (location.live_period is not None)

    trip = await get_active_or_planning_trip(user.id)
    if not trip:
        # No journey: still run geo-fencing & safety state updates.
        await safety_location_hook(update, context, lat, lng, None)
        if not is_live_stream:
            await location_snapshot_reply(update, lat, lng)
        return

    trip_id = trip["trip_id"]

    # If trip is in planning status, activate it!
    if trip["status"] == "planning":
        await activate_trip(trip_id, user.id)
        trip["status"] = "active"

    # Evaluate location ping through the tracking engine
    evaluation = process_location_ping(trip, lat, lng)

    logger.info(
        "User %s live ping | Event: %s | Dist: %.1fm | Speed: %s | ETA: %.1fm",
        user.id,
        evaluation.event,
        evaluation.distance_meters,
        f"{evaluation.speed_kmh:.1f}km/h" if evaluation.speed_kmh else "N/A",
        evaluation.eta_minutes,
    )

    # 1. Event: Tracking Started
    if evaluation.event == "TRACKING_STARTED":
        await msg.reply_html(
            f"🚀 <b>Live Journey Tracking Started!</b>\n\n"
            f"• <b>Destination:</b> <b>{evaluation.destination_name}</b>\n"
            f"• <b>Initial Distance:</b> <code>{evaluation.distance_km:.2f} km</code>\n"
            f"• <b>Estimated ETA:</b> ~<code>{int(round(evaluation.eta_minutes))} mins</code>\n\n"
            "🔔 I'll monitor your progress and alert you <b>3-5 minutes</b> before your stop arrives!"
        )

    # 2. Event: Approaching Stop Alert
    elif evaluation.event == "APPROACHING_ALERT":
        await mark_alert_sent(trip_id)
        speed_info = (
            f" (Speed: {evaluation.speed_kmh:.1f} km/h)"
            if evaluation.speed_kmh
            else ""
        )
        eta_display = max(1, int(round(evaluation.eta_minutes)))

        await msg.reply_html(
            f"🔔🔔 <b>APPROACHING YOUR STOP!</b> 🔔🔔\n\n"
            f"You are almost at <b>{evaluation.destination_name}</b>!\n\n"
            f"• <b>Distance Remaining:</b> <b>{evaluation.distance_km:.2f} km</b> ({evaluation.distance_meters:.0f}m)\n"
            f"• <b>Estimated Arrival:</b> <b>~{eta_display} minute(s)</b>{speed_info}\n\n"
            "🎒 <i>Please get your belongings together and prepare to alight safely!</i>"
        )

    # 3. Event: Arrived at Destination
    elif evaluation.event == "ARRIVED":
        await complete_trip(user.id)
        remove_tracker(user.id)
        await msg.reply_html(
            f"🎉 <b>Looks like you've arrived at {evaluation.destination_name}!</b>\n\n"
            f"Trip marked as <b>completed</b>.\n"
            f"• <b>Final Proximity:</b> <code>{evaluation.distance_meters:.0f} meters</code>\n\n"
            "Hope you had a comfortable and pleasant journey! 🚆✨"
        )

    # 4. Event: Destination Overshot
    elif evaluation.event == "OVERSHOT":
        await msg.reply_html(
            f"⚠️ <b>Notice: Destination Passed?</b>\n\n"
            f"You appear to be moving away from <b>{evaluation.destination_name}</b>.\n"
            f"• <b>Current Distance:</b> <code>{evaluation.distance_km:.2f} km</code>\n\n"
            "If you have already disembarked, you can conclude your trip with /endtrip."
        )

    # 5. Event: In Transit (Single non-live ping feedback)
    elif evaluation.event == "IN_TRANSIT" and not is_live_stream:
        speed_str = f"{evaluation.speed_kmh:.1f} km/h" if evaluation.speed_kmh else "calculating"
        await msg.reply_html(
            f"📍 <b>Trip Progress Update</b>\n\n"
            f"• <b>Destination:</b> {evaluation.destination_name}\n"
            f"• <b>Remaining:</b> <code>{evaluation.distance_km:.2f} km</code>\n"
            f"• <b>Speed:</b> <code>{speed_str}</code>\n"
            f"• <b>ETA:</b> ~<code>{int(round(evaluation.eta_minutes))} mins</code>\n\n"
            "💡 <i>Keep sharing Live Location for automatic proximity alerts before arrival!</i>"
        )

    # 6. Safety layer: geo-fencing, anomaly detection, check-in handling
    if evaluation.event == "ARRIVED":
        reset_motion(user.id)
        await safety_location_hook(update, context, lat, lng, None)
    else:
        tracker_state = _active_trackers.get(user.id)
        await safety_location_hook(
            update, context, lat, lng, trip,
            alert_already_sent=bool(tracker_state and tracker_state.alert_sent),
        )


# Backward compatibility alias
location_handler = live_location_handler


# ─────────────────────────────────────────────
#  /preferences Onboarding Flow (Phase 4)
# ─────────────────────────────────────────────

async def preferences_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /preferences onboarding conversation.
    """
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    await upsert_user(user.id, user.username or user.first_name)

    reply_keyboard = [
        ["🍲 Food & Dining", "🏛️ History & Culture"],
        ["🌿 Nature & Outdoors", "🎉 Nightlife & Entertainment"],
        ["🛍️ Shopping & Markets", "✨ Mixed Highlights"],
    ]

    await update.effective_message.reply_html(
        "✨ <b>Set Your Travel Preferences</b>\n\n"
        "To get personalized travel tips from the AI guide, tell me your travel style in 3 quick questions:\n\n"
        "<b>Step 1/3: What are your primary interests?</b>\n"
        "Select an option below or type your custom interests (e.g., <i>'craft beer, museums, street photography'</i>):\n\n"
        "<i>(Send /cancel anytime to abort)</i>",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return PREF_INTERESTS


async def pref_interests_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Stores interests and prompts for budget level.
    """
    raw_text = update.message.text.strip()
    interests = (
        raw_text.replace("🍲", "")
        .replace("🏛️", "")
        .replace("🌿", "")
        .replace("🎉", "")
        .replace("🛍️", "")
        .replace("✨", "")
        .strip()
    )
    context.user_data["pref_interests"] = interests or "food, sightseeing, local culture"

    reply_keyboard = [
        ["💰 Low / Budget-Friendly", "💳 Medium / Moderate", "💎 High / Luxury"]
    ]

    await update.effective_message.reply_html(
        f"Interests saved: <b>{interests}</b>\n\n"
        "<b>Step 2/3: What is your typical travel budget?</b>",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return PREF_BUDGET


async def pref_budget_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Stores budget level and prompts for travel pace.
    """
    text = update.message.text.strip().lower()
    if "low" in text or "budget" in text:
        budget = "low"
    elif "high" in text or "luxury" in text:
        budget = "high"
    else:
        budget = "medium"

    context.user_data["pref_budget"] = budget

    reply_keyboard = [
        ["☕ Relaxed (Take it easy)", "⚡ Packed (See as much as possible)"]
    ]

    await update.effective_message.reply_html(
        f"Budget set: <b>{budget.capitalize()}</b>\n\n"
        "<b>Step 3/3: What is your preferred travel pace?</b>\n"
        "• <b>Relaxed:</b> 2–3 highlights with plenty of leisure/cafe time\n"
        "• <b>Packed:</b> High-energy, see as many spots as possible",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return PREF_PACE


async def pref_pace_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Stores pace, persists user preferences to database, and handles any pending explore query.
    """
    user = update.effective_user
    text = update.message.text.strip().lower()

    if "pack" in text or "fast" in text:
        pace = "packed"
    else:
        pace = "relaxed"

    interests = context.user_data.get("pref_interests", "food, culture, sightseeing")
    budget = context.user_data.get("pref_budget", "medium")

    # Save to SQLite database
    await save_user_preferences(user.id, interests, budget, pace)

    pending_query = context.user_data.pop("pending_explore_query", None)
    context.user_data.clear()

    await update.effective_message.reply_html(
        "🎉 <b>Travel Preferences Saved!</b>\n\n"
        f"• <b>Interests:</b> {interests}\n"
        f"• <b>Budget:</b> {budget.capitalize()}\n"
        f"• <b>Pace:</b> {pace.capitalize()}\n\n"
        "You can update these anytime using /preferences.\n"
        "─────────────────────────",
        reply_markup=MAIN_KEYBOARD,
    )

    # If this onboarding was triggered while trying to /explore, execute the query now!
    if pending_query:
        prefs = {"interests": interests, "budget_level": budget, "pace": pace}
        await _execute_explore_query(update, user.id, pending_query, prefs)
    else:
        await update.effective_message.reply_html(
            "💡 <b>Ready to explore!</b> Ask me for recommendations anytime:\n"
            "• <i>/explore What should I see in Jaipur?</i>\n"
            "• <i>/explore Best local dinner spots</i>\n"
            "• <i>/explore Hidden gems near here</i>"
        )

    return ConversationHandler.END


async def cancel_pref_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Aborts the /preferences onboarding conversation.
    """
    context.user_data.clear()
    await update.effective_message.reply_html(
        "🚫 <b>Preferences setup cancelled.</b>",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  /explore Command Handler
# ─────────────────────────────────────────────

async def _execute_explore_query(
    update: Update, user_id: int, query: str, prefs: dict
) -> None:
    """
    Helper to run DuckDuckGo search + Groq LLM travel guide generation.
    """
    status_msg = await update.effective_message.reply_html(
        "🔍 <i>Searching fresh local info & generating personalized recommendations…</i>"
    )

    # Check for active or planned trip to inject destination context
    ongoing_trip = await get_active_or_planning_trip(user_id)
    destination_context = ongoing_trip["destination_name"] if ongoing_trip else None

    # Call AI Guide
    guide_text = await generate_travel_guide(
        user_query=query,
        destination_context=destination_context,
        preferences=prefs,
    )

    # Context tag
    header = ""
    if destination_context and destination_context.lower() not in query.lower():
        header = f"📍 <b>Guide for {destination_context} (Active Trip Context)</b>\n\n"

    try:
        await status_msg.edit_text(header + guide_text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        # Fallback to plain text if HTML tags inside LLM output were invalid
        await status_msg.edit_text(header + guide_text, disable_web_page_preview=True)


async def explore_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /explore command.
    Checks if preferences exist; if not, triggers onboarding.
    Otherwise queries DuckDuckGo and generates personalized Groq recommendations.
    """
    user = update.effective_user
    if not user:
        return

    await upsert_user(user.id, user.username or user.first_name)
    query = " ".join(context.args).strip() if context.args else ""

    prefs = await get_user_preferences(user.id)
    if not prefs:
        # Trigger one-time onboarding
        if query:
            context.user_data["pending_explore_query"] = query

        await update.effective_message.reply_html(
            "👋 <b>Welcome to Travel Buddy's AI Guide!</b>\n\n"
            "Before I can give you personalized recommendations, let's set your travel style in 3 quick questions.",
        )
        return await preferences_start(update, context)

    # If no query was passed with /explore
    if not query:
        ongoing_trip = await get_active_or_planning_trip(user.id)
        trip_hint = ""
        if ongoing_trip:
            trip_hint = f"\n💡 <i>You have an ongoing journey to <b>{ongoing_trip['destination_name']}</b>! Ask me:</i>\n• <code>/explore top places to visit</code>\n• <code>/explore best local food spots</code>\n"

        await update.effective_message.reply_html(
            "🗺️ <b>Travel Buddy — AI Travel Guide</b>\n\n"
            "Ask me anything about your destination! For example:\n"
            "• <code>/explore What should I see in Jaipur?</code>\n"
            "• <code>/explore Best coffee shops with Wi-Fi</code>\n"
            "• <code>/explore Hidden gems off the beaten path</code>\n"
            f"{trip_hint}"
        )
        return

    await _execute_explore_query(update, user.id, query, prefs)


# ─────────────────────────────────────────────
#  Global Error Handler
# ─────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Log all unhandled exceptions raised inside handlers.
    """
    logger.error("Unhandled exception while processing update:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong processing your request. Please try again in a moment."
            )
        except Exception:
            pass
