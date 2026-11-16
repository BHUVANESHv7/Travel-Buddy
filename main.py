"""
main.py — Entry point for Travel Buddy (Smart Tourist Safety Monitoring & Incident Response).

  - Loads configuration from .env
  - Initialises the database, safety schema and blockchain ledger (post_init)
  - Registers all Telegram handlers from travel_buddy.bot
  - Starts the background safety monitor and the web command center
  - Global error handling & dual logging (console + travel_buddy.log)

Run:  python main.py
"""

import asyncio
import logging
import logging.handlers
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()  # load before importing modules that read env vars at import time

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from travel_buddy.config import check_config
from travel_buddy.core.database import init_db
from travel_buddy.bot.handlers import (
    start_handler,
    help_handler,
    my_trip_handler,
    end_trip_handler,
    cancel_trip_handler,
    location_handler,
    error_handler,
    # Conversation handler steps & states
    new_trip_start,
    destination_name_received,
    transport_mode_received,
    destination_location_pin_received,
    destination_text_geocoded,
    cancel_trip_conversation,
    DESTINATION_NAME,
    TRANSPORT_MODE,
    DESTINATION_LOCATION,
    # Preferences & AI Explore (Phase 4)
    preferences_start,
    pref_interests_received,
    pref_budget_received,
    pref_pace_received,
    cancel_pref_conversation,
    explore_handler,
    PREF_INTERESTS,
    PREF_BUDGET,
    PREF_PACE,
)
from travel_buddy.bot import intel_handlers as ih
from travel_buddy.bot import safety_handlers as sh
from travel_buddy.web import dashboard as web_dashboard
from travel_buddy.safety.ledger import ensure_genesis
from travel_buddy.safety.incidents import admin_ids, monitor_loop
from travel_buddy.bot.keyboards import QUICK_BUTTONS
from travel_buddy.safety.store import init_safety_db


# ─────────────────────────────────────────────
#  Logging configuration
# ─────────────────────────────────────────────

def _log_handlers() -> list[logging.Handler]:
    """Console always; plus a rotating file (5 x 2 MB) unless LOG_FILE is set to an empty value."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = os.getenv("LOG_FILE", "travel_buddy.log").strip()
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    return handlers


logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers(),
)

# Suppress noisy third-party logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Main Application Setup
# ─────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("sos", "🆘 Emergency alert with your location"),
    BotCommand("safety", "Your safety score & status"),
    BotCommand("nearby", "Nearest police, hospitals & emergency numbers"),
    BotCommand("report", "Report an incident (AI triage)"),
    BotCommand("newtrip", "Plan a journey with arrival alerts"),
    BotCommand("mytrip", "Current trip, distance & ETA"),
    BotCommand("registerid", "Get a blockchain Digital Tourist ID"),
    BotCommand("myid", "Show your Digital Tourist ID"),
    BotCommand("addguardian", "Invite an emergency contact"),
    BotCommand("timer", "Safety check-in timer"),
    BotCommand("safe", "I'm okay — answer check-ins"),
    BotCommand("zones", "Risk zones around you"),
    BotCommand("safetytips", "AI safety briefing for a place"),
    BotCommand("ask", "🤖 AI safety copilot (or just type / send a voice note)"),
    BotCommand("saferoute", "🧭 Check how safe a route is"),
    BotCommand("weather", "🌩️ Weather hazards near you"),
    BotCommand("hotspots", "🔥 Learned incident hotspots"),
    BotCommand("phrases", "🗣️ Emergency phrases in local language"),
    BotCommand("community", "📢 Nearby incident alerts on/off"),
    BotCommand("login", "🌐 Sign in to your web portal"),
    BotCommand("web", "🖥️ Safety portal & command center"),
    BotCommand("explore", "AI travel recommendations"),
    BotCommand("incidents", "Your incidents"),
    BotCommand("help", "All commands"),
]


async def post_init(application: Application) -> None:
    await init_db()
    await init_safety_db()
    await ensure_genesis()
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.warning("Could not set bot command menu: %s", exc)
    application.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(application.bot))
    from travel_buddy.web import runtime as web_runtime
    web_runtime.attach(asyncio.get_running_loop(), application.bot)
    if os.getenv("WEB_ENABLED", "1") != "0" and web_dashboard.start_web_server():
        admin_link = f"{web_dashboard.public_base_url()}/command?token={web_dashboard.dashboard_token()}"
        for admin_id in admin_ids():
            try:
                await application.bot.send_message(
                    admin_id, f"🖥️ Travel Buddy Safety is online.\nCommand center: {admin_link}",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
    web_dashboard.stop_web_server()


def main() -> None:
    # 1. Validate configuration (.env is already loaded at import time)
    problems = check_config()
    for level, message in problems:
        (logger.critical if level == "error" else logger.warning)("Config: %s", message)
    if any(level == "error" for level, _ in problems):
        logger.critical("Fix the configuration above (see .env.example), then start again.")
        sys.exit(1)
    token = os.environ["BOT_TOKEN"].strip()

    logger.info("Travel Buddy Bot starting up…")
    app = build_application(token)
    logger.info("All handlers successfully registered. Starting polling…")

    # 7. Start polling
    app.run_polling(
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=True,
    )


def build_application(token: str) -> Application:
    """Create the Telegram application with every handler registered (does not start polling)."""
    # 2. Build the Application with DB initialisation post-init hook
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 2b. Quick safety buttons (SOS etc.) run before everything else, even mid-conversation
    quick_regex = "^(" + "|".join(re.escape(b) for b in QUICK_BUTTONS) + ")$"
    app.add_handler(MessageHandler(filters.Regex(quick_regex), sh.quick_button_handler), group=-1)
    app.add_handler(CommandHandler("sos", sh.sos_handler), group=-1)

    # 3. Register /newtrip ConversationHandler
    new_trip_conv = ConversationHandler(
        entry_points=[CommandHandler("newtrip", new_trip_start)],
        states={
            DESTINATION_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, destination_name_received)
            ],
            TRANSPORT_MODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, transport_mode_received)
            ],
            DESTINATION_LOCATION: [
                MessageHandler(filters.LOCATION, destination_location_pin_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, destination_text_geocoded),
            ],
        },
        fallbacks=[
            CommandHandler("canceltrip", cancel_trip_conversation),
            CommandHandler("cancel", cancel_trip_conversation),
        ],
    )
    app.add_handler(new_trip_conv)

    # 4. Register /preferences & /explore onboarding ConversationHandler (Phase 4)
    preferences_conv = ConversationHandler(
        entry_points=[
            CommandHandler("preferences", preferences_start),
            CommandHandler("explore", explore_handler),
        ],
        states={
            PREF_INTERESTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pref_interests_received)
            ],
            PREF_BUDGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pref_budget_received)
            ],
            PREF_PACE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pref_pace_received)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_pref_conversation),
        ],
    )
    app.add_handler(preferences_conv)

    # 5. Register standard command handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("mytrip", my_trip_handler))
    app.add_handler(CommandHandler("endtrip", end_trip_handler))
    app.add_handler(CommandHandler("canceltrip", cancel_trip_handler))
    app.add_handler(CommandHandler("explore", explore_handler))

    # 5b. Digital Tourist ID registration (blockchain)
    text = filters.TEXT & ~filters.COMMAND
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("registerid", sh.registerid_start)],
        states={
            sh.REG_NAME: [MessageHandler(text, sh.reg_name)],
            sh.REG_NATIONALITY: [MessageHandler(text, sh.reg_nationality)],
            sh.REG_DOC_TYPE: [MessageHandler(text, sh.reg_doc_type)],
            sh.REG_DOC_NUMBER: [MessageHandler(text, sh.reg_doc_number)],
            sh.REG_EMERGENCY: [MessageHandler(text, sh.reg_emergency)],
            sh.REG_DAYS: [MessageHandler(text, sh.reg_days)],
        },
        fallbacks=[CommandHandler("cancel", sh.cancel_conversation)],
    ))

    # 5c. Incident reporting
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("report", sh.report_start)],
        states={
            sh.REP_TYPE: [MessageHandler(text, sh.report_type)],
            sh.REP_DESC: [MessageHandler(text, sh.report_desc)],
            sh.REP_LOC: [
                MessageHandler(filters.LOCATION, sh.report_location_pin),
                MessageHandler(text, sh.report_location_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", sh.cancel_conversation)],
    ))

    # 5d. Safety, guardians, incidents & authority commands
    safety_commands = {
        "myid": sh.myid_handler,
        "verifyid": sh.verifyid_handler,
        "revokeid": sh.revokeid_handler,
        "ledger": sh.ledger_handler,
        "addguardian": sh.addguardian_handler,
        "guardians": sh.guardians_handler,
        "removeguardian": sh.removeguardian_handler,
        "safety": sh.safety_status_handler,
        "zones": sh.zones_handler,
        "nearby": sh.nearby_handler,
        "safetytips": sh.safetytips_handler,
        "timer": sh.timer_handler,
        "safe": sh.safe_handler,
        "monitoring": sh.monitoring_handler,
        "incidents": sh.incidents_handler,
        "resolve": sh.resolve_handler,
        "efir": sh.efir_handler,
        "dashboard": sh.dashboard_handler,
        "addzone": sh.addzone_handler,
        "removezone": sh.removezone_handler,
        "whereis": sh.whereis_handler,
    }
    # 5e. Next-gen intelligence: copilot, voice, evidence, routes, weather, hotspots, portal
    safety_commands.update({
        "ask": ih.ask_handler,
        "saferoute": ih.saferoute_handler,
        "weather": ih.weather_handler,
        "hotspots": ih.hotspots_handler,
        "phrases": ih.phrases_handler,
        "community": ih.community_handler,
        "web": ih.web_handler,
        "usage": ih.usage_handler,
        "login": ih.login_handler,
    })
    for name, handler in safety_commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(sh.safety_callback_handler, pattern=r"^(ci|inc):"))
    app.add_handler(CallbackQueryHandler(ih.timer_callback, pattern=r"^tm:\d+$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, ih.voice_handler))
    app.add_handler(MessageHandler(filters.PHOTO, ih.photo_handler))

    # 5. Catch-all location messages (outside of /newtrip conversation)
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))

    # 5f. Anything else typed outside a flow → AI Safety Copilot (must stay last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
                                   ih.free_text_handler))

    # 6. Global error handler
    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    main()
