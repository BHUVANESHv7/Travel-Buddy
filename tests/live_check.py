"""
live_check.py — End-to-end LIVE test against the real Telegram Bot API.

Feeds a scripted session (commands, conversations, location pings, live journey) into the
bot's real handlers as if a user typed them. Every reply is really sent through Telegram, so
it shows up in that user's chat in real time. Each Bot API call is recorded and any error fails
the step.

Requirements: BOT_TOKEN in .env, and the target user must have pressed START in the bot once.
Run (the normal bot may keep running alongside):

    python tests/live_check.py                  # uses the first ID in ADMIN_IDS
    python tests/live_check.py --chat-id 12345  # explicit chat

The run creates a test Digital ID / trip / incidents for that user and revokes the test ID at the end.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make `travel_buddy` importable

import argparse
import asyncio
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv(_Path(__file__).resolve().parents[1] / ".env")

# Keep the real database clean: the live run uses its own temporary database unless --real-db.
if "--real-db" not in sys.argv:
    import tempfile as _tempfile
    os.environ["DATABASE_PATH"] = os.path.join(_tempfile.mkdtemp(), "live_check.db")
    os.environ["SEED_STARTER_ZONES"] = "1"  # sample zones so the geo-fence steps have something to hit

from telegram import Update
from telegram.ext import ExtBot

import main as bot_main
from travel_buddy.core import database
from travel_buddy.core.tracker import remove_tracker
from travel_buddy.safety import store
from travel_buddy.safety.ledger import ensure_genesis, get_active_tourist_id

# ── record every Bot API call ───────────────────────────────────────────────
CALLS: list[tuple[str, str | None]] = []  # (endpoint, error or None)
_orig_post = ExtBot._post


async def _recording_post(self, endpoint, *args, **kwargs):
    try:
        result = await _orig_post(self, endpoint, *args, **kwargs)
        CALLS.append((endpoint, None))
        return result
    except Exception as exc:  # noqa: BLE001
        CALLS.append((endpoint, f"{type(exc).__name__}: {exc}"))
        raise

ExtBot._post = _recording_post

HANDLER_ERRORS: list[str] = []


class Session:
    def __init__(self, app, chat_id: int, first_name: str, pace: float):
        self.app, self.chat_id, self.first_name, self.pace = app, chat_id, first_name, pace
        self.update_id = int(time.time())
        self.message_id = 900000
        self.results: list[tuple[str, bool, str]] = []

    def _base(self, **extra) -> dict:
        self.update_id += 1
        self.message_id += 1
        msg = {
            "message_id": self.message_id, "date": int(time.time()),
            "chat": {"id": self.chat_id, "type": "private", "first_name": self.first_name},
            "from": {"id": self.chat_id, "is_bot": False, "first_name": self.first_name},
        }
        msg.update(extra)
        return msg

    async def _feed(self, payload: dict) -> None:
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    async def text(self, text: str) -> None:
        extra = {"text": text}
        if text.startswith("/"):
            cmd = text.split()[0]
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        await self._feed({"update_id": self.update_id + 1, "message": self._base(**extra)})

    async def location(self, lat: float, lng: float, live: bool = False) -> None:
        loc = {"latitude": lat, "longitude": lng}
        if live:
            loc["live_period"] = 3600
            msg = self._base(location=loc, edit_date=int(time.time()))
            await self._feed({"update_id": self.update_id + 1, "edited_message": msg})
        else:
            await self._feed({"update_id": self.update_id + 1, "message": self._base(location=loc)})

    async def step(self, name: str, *actions, expect: str | None = None, min_calls: int = 1) -> None:
        start_calls, start_errs = len(CALLS), len(HANDLER_ERRORS)
        for kind, *args in actions:
            await getattr(self, kind)(*args)
            await asyncio.sleep(self.pace)
        await asyncio.sleep(0.5)  # let background error handlers finish before judging the step
        calls = CALLS[start_calls:]
        # Deleting the user's own (simulated) message can't succeed in a test: those messages don't exist.
        api_errors = [f"{e}: {err}" for e, err in calls
                      if err and not (e == "deleteMessage" and "not found" in err)]
        errs = [e for e in HANDLER_ERRORS[start_errs:] if "Message to delete not found" not in e] + api_errors
        sent = [e for e, err in calls if not err and e.startswith(("send", "edit"))]
        ok = not errs and len(sent) >= min_calls
        if ok and expect:
            ok = any(e == expect for e, _ in calls)
            if not ok:
                errs.append(f"expected a {expect} call")
        detail = f"{len(sent)} msg" + (f" · {', '.join(sorted(set(sent)))}" if sent else "")
        self.results.append((name, ok, "; ".join(errs) if errs else detail))
        print(f"{'✅' if ok else '❌'} {name:42} {self.results[-1][2]}")


async def run(chat_id: int, pace: float) -> int:
    app = bot_main.build_application(os.environ["BOT_TOKEN"])

    async def record_error(update, context):
        HANDLER_ERRORS.append(f"{type(context.error).__name__}: {context.error}")
    app.error_handlers.clear()
    app.add_error_handler(record_error)

    await app.initialize()
    await database.init_db()
    await store.init_safety_db()
    await ensure_genesis()

    me = await app.bot.get_me()
    chat = await app.bot.get_chat(chat_id)
    print(f"Live test: @{me.username} → {chat.first_name} ({chat_id}), pace {pace}s\n")
    s = Session(app, chat_id, chat.first_name or "Tester", pace)

    # clean slate for this user's live state
    old = await get_active_tourist_id(chat_id)
    if old:
        from travel_buddy.safety.ledger import revoke_tourist_id
        await revoke_tourist_id(old["tourist_id"], "live_test_reset")
    await database.cancel_trip(chat_id)
    remove_tracker(chat_id)

    await app.bot.send_message(chat_id, "🧪 <b>Automated live test starting</b> — the following messages are a scripted "
                               "end-to-end check of every feature. A test Digital ID will be revoked at the end.",
                               parse_mode="HTML")

    # ── Core ──
    await s.step("/start", ("text", "/start"))
    await s.step("/help", ("text", "/help"))

    # ── Digital ID (blockchain) ──
    await s.step("/registerid full flow + QR", ("text", "/registerid"), ("text", "Live Test Tourist"),
                 ("text", "Indian"), ("text", "Passport"), ("text", "LIVE1234"),
                 ("text", "Test Contact, +91 90000 00000"), ("text", "3"), expect="sendPhoto")
    tid = (await get_active_tourist_id(chat_id) or {}).get("tourist_id", "TID-MISSING")
    await s.step("/myid (card + QR)", ("text", "/myid"), expect="sendPhoto")
    await s.step("/verifyid", ("text", f"/verifyid {tid}"))
    await s.step("/ledger", ("text", "/ledger"))

    # ── Location, geo-fence, intelligence ──
    await s.step("location pin → geo-fence entry", ("location", 26.9239, 75.8267))
    await s.step("/safety (score + live weather)", ("text", "/safety"))
    await s.step("/nearby (OpenStreetMap services)", ("text", "/nearby"))
    await s.step("/weather", ("text", "/weather"))
    await s.step("/zones", ("text", "/zones"))
    await s.step("/hotspots", ("text", "/hotspots"))
    await s.step("/saferoute (OSRM road route)", ("text", "/saferoute Hawa Mahal, Jaipur"))
    await s.step("/phrases (auto language)", ("text", "/phrases"))
    await s.step("/phrases tamil", ("text", "/phrases tamil"))
    await s.step("AI copilot (free text)", ("text", "is it safe to walk to Hawa Mahal now?"))
    await s.step("copilot distress detection", ("text", "help me please someone is following me"))
    await s.step("/safetytips", ("text", "/safetytips Jaipur"))

    # ── Journey with live tracking ──
    await s.step("/newtrip wizard (geocoded)", ("text", "/newtrip"), ("text", "Jaipur Junction"),
                 ("text", "🚆 Train"), ("text", "Jaipur Junction railway station"))
    trip = await database.get_active_or_planning_trip(chat_id)
    if trip:
        dl, dg = trip["destination_lat"], trip["destination_lng"]
        await s.step("live location: tracking starts", ("location", dl + 0.045, dg, True))
        await s.step("live location: stop alert (<1 km)", ("location", dl + 0.008, dg, True))
        await s.step("live location: arrival (<150 m)", ("location", dl + 0.0008, dg, True))
    else:
        s.results.append(("/newtrip created a trip", False, "no trip found"))
        print("❌ /newtrip created a trip")
    await s.step("/mytrip after arrival", ("text", "/mytrip"))

    # ── Incidents ──
    await s.step("/report flow (AI triage)", ("text", "/report"), ("text", "👜 Theft / Loss"),
                 ("text", "My wallet was stolen near the station entrance"),
                 ("text", "📌 Use my last known location"))
    reported = [i for i in await store.get_user_incidents(chat_id, 5) if i["source"] == "report"]
    s.results.append(("report saved as incident", bool(reported),
                      f"#{reported[0]['incident_id']} {reported[0]['incident_type']}" if reported else "no incident saved"))
    print(f"{'✅' if reported else '❌'} {'report saved as incident':42} {s.results[-1][2]}")
    await s.step("/incidents", ("text", "/incidents"))
    last = (await store.get_user_incidents(chat_id, 1) or [{}])[0].get("incident_id", 0)
    await s.step("/efir (police report document)", ("text", f"/efir {last}"), expect="sendDocument")
    await s.step("/timer 30", ("text", "/timer 30"))
    await s.step("/sos (alerts admin + numbers)", ("text", "/sos"))
    await s.step("/safe (closes SOS)", ("text", "/safe"))
    await s.step("keyboard button: Safety status", ("text", "🛡️ Safety status"))

    # ── Guardians, community, admin ──
    await s.step("/addguardian (invite link)", ("text", "/addguardian Family"))
    await s.step("/guardians", ("text", "/guardians"))
    await s.step("/community", ("text", "/community"))
    await s.step("/monitoring", ("text", "/monitoring"))
    await s.step("/dashboard (admin)", ("text", "/dashboard"))
    await s.step("/incidents all (admin)", ("text", "/incidents all"))
    await s.step("/web (portal links)", ("text", "/web"))

    # ── AI travel guide with preferences onboarding ──
    await s.step("/explore + preferences wizard", ("text", "/explore best street food"),
                 ("text", "🍲 Food & Dining"), ("text", "💰 Low / Budget-Friendly"),
                 ("text", "☕ Relaxed (Take it easy)"))

    # ── Cleanup ──
    await s.step("/revokeid (cleanup test ID)", ("text", "/revokeid"))

    passed = sum(1 for _, ok, _ in s.results if ok)
    total = len(s.results)
    summary = f"🧪 <b>Live test finished:</b> {passed}/{total} checks passed."
    await app.bot.send_message(chat_id, summary, parse_mode="HTML")
    await app.shutdown()
    print(f"\n{'=' * 60}\nLIVE RESULT: {passed}/{total} passed")
    for name, ok, detail in s.results:
        if not ok:
            print(f"  ❌ {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", type=int, default=None)
    ap.add_argument("--pace", type=float, default=1.2, help="seconds between steps (Telegram rate limits)")
    ap.add_argument("--real-db", action="store_true", help="use the real travel_bot.db instead of a temporary one")
    a = ap.parse_args()
    cid = a.chat_id or next((int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()), None)
    if not cid:
        sys.exit("No chat id: pass --chat-id or set ADMIN_IDS in .env")
    sys.exit(asyncio.run(run(cid, a.pace)))
