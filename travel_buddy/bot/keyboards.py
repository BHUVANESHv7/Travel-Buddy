"""keyboards.py — Shared reply keyboards."""

from telegram import ReplyKeyboardMarkup

BTN_SOS = "🆘 SOS"
BTN_SAFETY = "🛡️ Safety status"
BTN_NEARBY = "🏥 Nearby help"
BTN_MYID = "🪪 My Tourist ID"

QUICK_BUTTONS = [BTN_SOS, BTN_SAFETY, BTN_NEARBY, BTN_MYID]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_SOS, BTN_SAFETY], [BTN_NEARBY, BTN_MYID]],
    resize_keyboard=True,
    is_persistent=True,
)
