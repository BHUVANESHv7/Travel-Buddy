"""
triage.py — AI incident triage and destination safety briefings (Groq + DuckDuckGo).

  - classify_incident(): turns a free-text report into {category, severity, advice}.
    Uses Groq in JSON mode; falls back to a keyword model when no API key / offline,
    so incident reporting always works.
  - generate_safety_briefing(): grounded safety advisory for a destination
    (scams, unsafe areas, emergency numbers, local norms) from fresh web results.
"""

import json
import logging
import os
import re

from groq import AsyncGroq

from travel_buddy.core.usage import try_consume

from travel_buddy.ai.travel_guide import DEFAULT_GROQ_MODEL, search_travel_web, format_search_snippets

logger = logging.getLogger(__name__)

CATEGORIES = ["medical", "theft", "harassment", "accident", "lost", "scam", "natural_hazard", "other"]
SEVERITIES = ["low", "medium", "high", "critical"]

KEYWORD_RULES = [
    # (category, severity, keywords)
    ("medical", "critical", ["unconscious", "not breathing", "heart attack", "seizure", "heavy bleeding",
                              "stroke", "overdose", "drowning"]),
    ("harassment", "critical", ["assault", "rape", "kidnap", "abduct", "weapon", "knife", "gun",
                                 "attacked", "being followed"]),
    ("accident", "high", ["accident", "crash", "collision", "fell", "fall", "injured", "fracture", "hit by"]),
    ("natural_hazard", "high", ["landslide", "flood", "earthquake", "avalanche", "fire", "storm", "tsunami"]),
    ("medical", "high", ["bleeding", "chest pain", "can't breathe", "allergic", "poison", "fever", "vomit"]),
    ("lost", "high", ["missing", "lost my child", "can't find", "lost contact"]),
    ("harassment", "medium", ["harass", "threat", "stalk", "abuse", "groped", "eve teasing", "intimidat"]),
    ("theft", "medium", ["stolen", "theft", "robbed", "pickpocket", "snatch", "passport lost", "wallet"]),
    ("scam", "low", ["scam", "overcharg", "cheat", "fraud", "fake guide", "fake ticket", "tout"]),
    ("lost", "medium", ["lost", "stranded", "wrong way", "don't know where"]),
]

DEFAULT_ADVICE = {
    "medical": "Call the ambulance number now. Stay with the person, keep them still and warm; do not give food or water if unconscious.",
    "theft": "Move to a safe public place. Block cards/phone, note the time and place, and file a report at the nearest police station (needed for insurance/embassy).",
    "harassment": "Go to a crowded, well-lit place or a shop. Call police if you feel threatened. Your guardians have been informed.",
    "accident": "Get away from traffic or hazards. Call emergency services, check for injuries and do not move anyone with neck/back pain.",
    "lost": "Stay where you are if it is safe and keep your phone on. Share your live location so guardians and responders can find you.",
    "scam": "Stop any further payment, keep receipts/screenshots, and report to the tourist police or helpline.",
    "natural_hazard": "Move to higher/open ground away from slopes, rivers or buildings, and follow local authority instructions.",
    "other": "Stay in a safe, public place and keep your phone charged. Use /sos if the situation gets worse.",
}

TRIAGE_SYSTEM_PROMPT = """You are an emergency triage assistant for a tourist safety service.
Classify the tourist's incident report and give short, practical first-response advice.
Respond ONLY with a JSON object:
{"category": one of %s,
 "severity": one of %s,
 "advice": "2-3 short sentences of immediate, safe, practical steps (plain text, no markdown)"}
Severity guide: critical = life or physical safety at immediate risk; high = injury, accident, missing person,
hazard; medium = theft, harassment without immediate danger, stranded; low = scams, minor issues.
If in doubt, choose the higher severity.""" % (CATEGORIES, SEVERITIES)


def _groq_key() -> str | None:
    """Groq key for ONE upcoming AI call, or None → use the free offline fallback.

    Returns None when no key is configured or today's free-tier cap (AI_DAILY_LIMIT) is used up,
    so the bot can never run past Groq's free limits."""
    key = os.getenv("GROQ_API_KEY")
    if not key or key.startswith("gsk_your") or key == "your_groq_api_key_here":
        return None
    if not try_consume("ai"):
        return None
    return key


def keyword_triage(description: str, hinted_category: str | None = None) -> dict:
    text = description.lower()
    for category, severity, words in KEYWORD_RULES:
        if any(w in text for w in words):
            break
    else:
        category = hinted_category if hinted_category in CATEGORIES else "other"
        severity = {"medical": "high", "accident": "high", "lost": "medium", "harassment": "medium",
                    "theft": "medium", "natural_hazard": "high", "scam": "low"}.get(category, "medium")
    # A user-picked category is more reliable than a keyword guess for the category itself.
    if hinted_category in CATEGORIES and hinted_category != "other":
        category = hinted_category
    return {"category": category, "severity": severity,
            "advice": DEFAULT_ADVICE[category], "engine": "rules"}


async def classify_incident(description: str, hinted_category: str | None = None) -> dict:
    baseline = keyword_triage(description, hinted_category)
    key = _groq_key()
    if not key:
        return baseline
    try:
        client = AsyncGroq(api_key=key)
        resp = await client.chat.completions.create(
            model=DEFAULT_GROQ_MODEL,
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Tourist-selected category: {hinted_category or 'none'}\n"
                                            f"Report: {description}"},
            ],
            temperature=0.1,
            max_tokens=250,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        category = data.get("category") if data.get("category") in CATEGORIES else baseline["category"]
        severity = data.get("severity") if data.get("severity") in SEVERITIES else baseline["severity"]
        # Never let the model down-grade below the rule-based floor.
        if SEVERITIES.index(severity) < SEVERITIES.index(baseline["severity"]):
            severity = baseline["severity"]
        advice = re.sub(r"<[^>]+>", "", str(data.get("advice") or baseline["advice"]))[:600]
        return {"category": category, "severity": severity, "advice": advice, "engine": "groq"}
    except Exception as exc:
        logger.warning("AI triage failed, using rule-based triage: %s", exc)
        return baseline


SAFETY_BRIEFING_PROMPT = """You are Travel Buddy Safety, a calm, factual tourist-safety advisor.
Write a safety briefing for the destination using the web results provided.
Structure (Telegram HTML: <b>, <i>, bullet •):
<b>🛡️ Overall</b> one line on general safety level.
<b>⚠️ Watch out for</b> 3-4 bullets: common scams, risky areas/times, natural or health hazards.
<b>✅ Do</b> 3 bullets of practical precautions and local norms.
<b>☎️ Emergency</b> the local emergency/tourist-police numbers if found in the results.
Rules: only state specifics supported by the search results; otherwise give general, clearly-worded advice.
Never invent phone numbers. 150-250 words."""


async def generate_safety_briefing(place: str, extra_context: str = "") -> str:
    key = _groq_key()
    results = await search_travel_web(f"{place} tourist safety tips scams advisory", max_results=5)
    if not key:
        lines = [f"🛡️ <b>Safety briefing: {place}</b>",
                 "<i>(AI summary unavailable — set GROQ_API_KEY. Showing fresh web results.)</i>", ""]
        for r in results[:4]:
            lines.append(f"• <b>{r['title'][:80]}</b>\n  {r['snippet'][:200]}")
        if not results:
            lines.append("• Keep valuables hidden, use registered taxis/guides, avoid isolated areas after dark, "
                         "and share your live location with a guardian.")
        return "\n".join(lines)
    try:
        client = AsyncGroq(api_key=key)
        resp = await client.chat.completions.create(
            model=DEFAULT_GROQ_MODEL,
            messages=[
                {"role": "system", "content": SAFETY_BRIEFING_PROMPT},
                {"role": "user", "content": f"Destination: {place}\n{extra_context}\n\n"
                                            f"WEB RESULTS:\n{format_search_snippets(results)}"},
            ],
            temperature=0.3,
            max_tokens=550,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.error("Safety briefing generation failed: %s", exc)
        return f"⚠️ Could not generate a safety briefing right now ({exc}). Please try again later."
