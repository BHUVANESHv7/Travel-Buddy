"""
ai_copilot.py — Context-aware AI Safety Copilot, voice understanding and emergency phrasebook.

  - answer_copilot(): the tourist just chats ("is it safe to walk to the fort now?") and the
    copilot answers using their LIVE situation: place, zone, safety score, weather hazards,
    nearby incident hotspots, active trip and open incidents. Replies in the user's language.
  - transcribe_voice(): Groq Whisper speech-to-text (any language) for voice SOS / voice reports.
  - detect_distress(): multilingual distress-phrase detector used before any AI call, so a
    voice note like "bachao!" or "help me" triggers an SOS even when the AI is offline.
  - PHRASEBOOK: offline emergency phrases; translate_phrase() adds AI translation for anything else.
"""

import logging
import re

from groq import AsyncGroq

from ai_guide import DEFAULT_GROQ_MODEL
from ai_safety import _groq_key

logger = logging.getLogger(__name__)

WHISPER_MODEL = "whisper-large-v3-turbo"

# Words that signal immediate danger, in several languages (lower-case, romanised + native script).
DISTRESS_PATTERNS = [
    r"\bhelp me\b", r"\bhelp\b.*\bplease\b", r"\bsave me\b", r"\bemergency\b", r"\bsos\b",
    r"\bcall (the )?police\b", r"\bcall (an )?ambulance\b", r"\bi('m| am) (being )?(attacked|followed|kidnapped)\b",
    r"\bbachao\b", r"\bbacha lo\b", r"बचाओ", r"मदद", r"\bmadad\b",                 # Hindi
    r"বাঁচাও", r"সাহায্য", r"\bbachao\b",                                            # Bengali
    r"காப்பாத்துங்க", r"உதவி",                                                      # Tamil
    r"\bsocorro\b", r"\bayuda\b", r"\bau secours\b", r"\baidez[- ]moi\b", r"\bhilfe\b",  # PT/ES/FR/DE
    r"助けて", r"救命",                                                                # JA / ZH
]


def detect_distress(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in DISTRESS_PATTERNS)


# ─────────────────────────────────────────────
#  Voice
# ─────────────────────────────────────────────

async def transcribe_voice(audio: bytes, filename: str = "voice.ogg") -> dict:
    """Returns {'text', 'language', 'ok', 'error'}."""
    key = _groq_key()
    if not key:
        return {"ok": False, "text": "", "error": "GROQ_API_KEY not configured"}
    try:
        client = AsyncGroq(api_key=key)
        resp = await client.audio.transcriptions.create(
            file=(filename, audio), model=WHISPER_MODEL, response_format="verbose_json",
        )
        text = getattr(resp, "text", "") or ""
        language = getattr(resp, "language", None)
        if language is None and isinstance(resp, dict):
            language = resp.get("language")
        return {"ok": True, "text": text.strip(), "language": language}
    except Exception as exc:
        logger.warning("Voice transcription failed: %s", exc)
        return {"ok": False, "text": "", "error": str(exc)}


# ─────────────────────────────────────────────
#  Copilot
# ─────────────────────────────────────────────

COPILOT_SYSTEM = """You are Travel Buddy Copilot, a calm, practical AI safety companion for tourists.
You know the traveller's LIVE situation (below). Use it — refer to their actual zone, weather,
nearby hotspots and score when relevant. Rules:
- If anything suggests immediate danger, tell them FIRST to press /sos or call the emergency number given.
- Be concrete and short (max ~150 words), Telegram HTML only (<b>, <i>), bullets with •.
- Never invent phone numbers, prices or facts not in the context; say "check locally" instead.
- Reply in the same language the traveller wrote in.
- Useful bot commands you may suggest: /sos /nearby /saferoute <place> /weather /hotspots /timer <min>
  /report /phrases /safetytips <place> /registerid /addguardian."""


def build_context_text(ctx: dict) -> str:
    lines = [f"- {k}: {v}" for k, v in ctx.items() if v not in (None, "", [], {})]
    return "TRAVELLER LIVE CONTEXT:\n" + "\n".join(lines)


async def answer_copilot(question: str, ctx: dict) -> str:
    key = _groq_key()
    if not key:
        # Useful offline answer built from the same live context.
        parts = ["🤖 <b>Copilot (offline mode)</b> — AI key not configured, here's your live picture:"]
        for label in ("place", "safety_score", "zone", "weather", "weather_hazards", "nearby_hotspots", "trip"):
            if ctx.get(label):
                parts.append(f"• <b>{label.replace('_', ' ').title()}:</b> {ctx[label]}")
        parts.append("\nTry /saferoute &lt;place&gt;, /nearby or /weather. In danger → /sos.")
        return "\n".join(parts)
    try:
        client = AsyncGroq(api_key=key)
        resp = await client.chat.completions.create(
            model=DEFAULT_GROQ_MODEL,
            messages=[
                {"role": "system", "content": COPILOT_SYSTEM},
                {"role": "user", "content": f"{build_context_text(ctx)}\n\nTRAVELLER SAYS:\n{question}"},
            ],
            temperature=0.4,
            max_tokens=400,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.error("Copilot failed: %s", exc)
        return f"⚠️ Copilot is unavailable right now ({exc}). In danger? Press /sos."


# ─────────────────────────────────────────────
#  Emergency phrasebook
# ─────────────────────────────────────────────

PHRASE_KEYS = ["Help!", "Call the police!", "I need a doctor.", "Where is the hospital?", "I am lost."]

PHRASEBOOK = {
    "hindi": ["मदद करो! (madad karo!)", "पुलिस को बुलाओ! (police ko bulao!)",
              "मुझे डॉक्टर चाहिए। (mujhe doctor chahiye)", "अस्पताल कहाँ है? (aspataal kahan hai?)",
              "मैं रास्ता भटक गया/गई हूँ। (main raasta bhatak gaya/gayi hoon)"],
    "bengali": ["সাহায্য করুন! (sahajjo korun!)", "পুলিশ ডাকুন! (pulish dakun!)",
                "আমার ডাক্তার দরকার। (amar daktar dorkar)", "হাসপাতাল কোথায়? (haspatal kothay?)",
                "আমি পথ হারিয়ে ফেলেছি। (ami poth hariye phelechhi)"],
    "tamil": ["உதவி! (udhavi!)", "காவல்துறையை அழையுங்கள்! (kaavalthuraiyai azhaiyungal!)",
              "எனக்கு மருத்துவர் வேண்டும். (enakku maruthuvar vendum)", "மருத்துவமனை எங்கே? (maruthuvamanai enge?)",
              "நான் வழி தவறிவிட்டேன். (naan vazhi thavarivitten)"],
    "spanish": ["¡Ayuda!", "¡Llame a la policía!", "Necesito un médico.", "¿Dónde está el hospital?", "Estoy perdido/perdida."],
    "french": ["Au secours !", "Appelez la police !", "J'ai besoin d'un médecin.", "Où est l'hôpital ?", "Je suis perdu/perdue."],
    "japanese": ["助けて！(tasukete!)", "警察を呼んでください！(keisatsu o yonde kudasai!)",
                 "医者が必要です。(isha ga hitsuyō desu)", "病院はどこですか？(byōin wa doko desu ka?)",
                 "道に迷いました。(michi ni mayoimashita)"],
}
COUNTRY_LANGUAGE = {"in": "hindi", "bd": "bengali", "lk": "tamil", "es": "spanish", "mx": "spanish",
                    "ar": "spanish", "fr": "french", "be": "french", "jp": "japanese"}


def phrasebook_text(language: str) -> str | None:
    rows = PHRASEBOOK.get(language.lower())
    if not rows:
        return None
    lines = [f"🗣️ <b>Emergency phrases — {language.title()}</b>", ""]
    for en, local in zip(PHRASE_KEYS, rows):
        lines.append(f"• <i>{en}</i>\n   <b>{local}</b>")
    return "\n".join(lines)


async def translate_phrase(text: str, language: str) -> str:
    key = _groq_key()
    if not key:
        return "⚠️ Custom translation needs GROQ_API_KEY. Built-in languages: " + ", ".join(PHRASEBOOK)
    try:
        client = AsyncGroq(api_key=key)
        resp = await client.chat.completions.create(
            model=DEFAULT_GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Translate the traveller's phrase for showing to a local person. "
                 "Output exactly two lines: the translation in native script, then a simple romanised "
                 "pronunciation in parentheses. No other text."},
                {"role": "user", "content": f"Target language: {language}\nPhrase: {text}"},
            ],
            temperature=0.1,
            max_tokens=120,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return f"⚠️ Translation failed: {exc}"
