"""
ai_guide.py — AI-Powered Travel Guide Service using Groq LLM & DuckDuckGo Search.

Architecture:
1. Web Search Engine:
   - Uses DuckDuckGo Search (via ddgs / duckduckgo_search) to retrieve fresh real-time
     attractions, restaurants, opening details, and recent local travel blogs.
   - Executed asynchronously via asyncio.to_thread to prevent blocking the event loop.

2. Context Resolution:
   - If the user has an active or planned trip, the destination (e.g. 'Jaipur') is
     automatically injected as context if the user's question is location-agnostic
     (e.g., "Good food spots near here" or "What should I see?").

3. Personalized Generation:
   - Stored user preferences (interests, budget level, and travel pace) are passed
     alongside search snippets to the Groq LLM.
   - Tailored system & user prompts instruct the model to produce concise, actionable,
     and grounded suggestions (avoiding hallucinated prices or hours).
"""

import asyncio
import logging
import os
from typing import Any

# Resilient import for DuckDuckGo search library
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS  # type: ignore

from groq import AsyncGroq

logger = logging.getLogger(__name__)

# Default fast and capable model on Groq
DEFAULT_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# ─────────────────────────────────────────────
#  Prompt Templates
# ─────────────────────────────────────────────

TRAVEL_GUIDE_SYSTEM_PROMPT = """You are Travel Buddy, an expert, thoughtful, and highly practical local travel concierge.
Your mission is to provide personalized, high-value travel recommendations tailored precisely to the traveler's stated preferences and journey context.

CORE INSTRUCTIONS:
1. PERSONALIZATION FIRST:
   - Strictly honor the traveler's profile:
     • Interests: {interests}
     • Budget Level: {budget} (Low: budget-friendly, street food, free sights; Medium: boutique, quality bistros, popular sights; High: luxury, fine dining, exclusive tours)
     • Pace: {pace} (Relaxed: 2-3 focused highlights with downtime; Packed: energetic, high-yield multi-stop highlights)
   - Do NOT output generic "Top 10" lists. Pick the best 3-4 curated recommendations that directly match their profile.

2. GROUNDED IN SEARCH RESULTS:
   - Incorporate the fresh, real-world context provided in the web search results.
   - Ground recommendations in real venues, neighborhoods, and actual experiences.
   - Do NOT fabricate specific factual details like exact admission prices, train ticket fares, or opening hours unless explicitly confirmed in the search snippets. If uncertain, state approximations (e.g., "roughly ~€10") and suggest verifying on-site.

3. CONCISE & READABLE FOR TELEGRAM:
   - Use clean, standard Telegram HTML formatting (<b>bold</b>, <i>italic</i>, <code>code</code>, bullet points).
   - Keep answers punchy, actionable, and structured (e.g., 📍 Highlight, 💡 Why It Fits You, 🎒 Pro-Tip).
   - Keep length between 200–350 words for optimal mobile reading.
"""

USER_PROMPT_TEMPLATE = """TRAVELER CONTEXT:
• Destination Context: {destination_context}
• Traveler Interests: {interests}
• Budget Level: {budget}
• Travel Pace: {pace}

TRAVELER QUESTION:
"{user_query}"

RECENT WEB SEARCH RESULTS:
{search_snippets}

Please provide a personalized, practical recommendation answering the traveler's question based on their preferences and the fresh search results.
"""


# ─────────────────────────────────────────────
#  DuckDuckGo Web Search
# ─────────────────────────────────────────────

def _sync_ddgs_search(query: str, max_results: int = 4) -> list[dict[str, str]]:
    """
    Synchronous helper to execute DuckDuckGo search.
    Called inside asyncio.to_thread.
    """
    try:
        ddgs_client = DDGS()
        raw_results = list(ddgs_client.text(query, max_results=max_results))
        formatted = []
        for r in raw_results:
            title = r.get("title", "").strip()
            snippet = r.get("body", "").strip()
            url = r.get("href", "").strip()
            if title and snippet:
                formatted.append({"title": title, "snippet": snippet, "url": url})
        return formatted
    except Exception as exc:
        logger.warning("DuckDuckGo search error for '%s': %s", query, exc)
        return []


async def search_travel_web(query: str, max_results: int = 4) -> list[dict[str, str]]:
    """
    Execute DuckDuckGo search non-blockingly via a background thread.
    """
    return await asyncio.to_thread(_sync_ddgs_search, query, max_results=max_results)


def format_search_snippets(results: list[dict[str, str]]) -> str:
    """
    Format web search results into a clean string for the LLM prompt.
    """
    if not results:
        return "No real-time web results found. Use your general knowledge while adhering to all grounding guidelines."

    snippets = []
    for i, res in enumerate(results, 1):
        snippets.append(f"[{i}] {res['title']}\nSummary: {res['snippet']}")
    return "\n\n".join(snippets)


# ─────────────────────────────────────────────
#  Groq Recommendation Engine
# ─────────────────────────────────────────────

async def generate_travel_guide(
    user_query: str,
    destination_context: str | None,
    preferences: dict[str, Any],
    api_key: str | None = None,
    model: str = DEFAULT_GROQ_MODEL,
) -> str:
    """
    Synthesize user query, destination context, web search, and user preferences into a personalized recommendation.
    """
    # 1. Verify Groq API Key
    groq_key = api_key or os.getenv("GROQ_API_KEY")
    if not groq_key or groq_key == "your_groq_api_key_here":
        return (
            "⚠️ <b>Groq API Key Not Configured</b>\n\n"
            "To use the AI travel guide, please add your Groq API key to the <code>.env</code> file:\n"
            "<code>GROQ_API_KEY=gsk_your_key_here</code>\n\n"
            "You can get a free key from <a href='https://console.groq.com/'>console.groq.com</a>."
        )

    # 2. Refine search query with destination context if applicable
    search_query = user_query.strip()
    dest_str = destination_context.strip() if destination_context else None

    # If the user's question doesn't explicitly name a city/place, inject destination
    if dest_str and dest_str.lower() not in search_query.lower():
        search_query = f"{dest_str} {search_query}"

    logger.info("Executing web search for query: '%s'", search_query)
    search_results = await search_travel_web(search_query, max_results=4)
    search_snippets = format_search_snippets(search_results)

    # 3. Extract user preferences
    interests = preferences.get("interests", "general sightseeing, local culture")
    budget = preferences.get("budget_level", "medium").capitalize()
    pace = preferences.get("pace", "balanced").capitalize()
    effective_dest = dest_str or "Not specified in journey (use location from question)"

    # 4. Construct prompts
    system_prompt = TRAVEL_GUIDE_SYSTEM_PROMPT.format(
        interests=interests,
        budget=budget,
        pace=pace,
    )

    user_prompt = USER_PROMPT_TEMPLATE.format(
        destination_context=effective_dest,
        interests=interests,
        budget=budget,
        pace=pace,
        user_query=user_query,
        search_snippets=search_snippets,
    )

    # 5. Invoke Groq LLM
    try:
        groq_client = AsyncGroq(api_key=groq_key)
        chat_completion = await groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.6,
            max_tokens=650,
        )

        reply = chat_completion.choices[0].message.content or ""
        return reply.strip()

    except Exception as exc:
        logger.error("Groq API error during travel guide generation: %s", exc)
        return (
            "⚠️ <b>AI Travel Guide Service Notice</b>\n\n"
            f"Could not generate recommendation: {exc}\n"
            "Please check your <code>GROQ_API_KEY</code> and internet connection, then try again."
        )
