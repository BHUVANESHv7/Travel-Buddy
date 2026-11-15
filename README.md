# 🚆 Travel Buddy — Smart Telegram Bot

A smart travel companion bot that tracks your live location during train/bus journeys,
alerts you before your stop arrives, and provides personalized AI recommendations powered by Groq and real-time DuckDuckGo web search.

---

## 🌟 Feature Overview

- **Phase 1:** Account registration, SQLite database (`users`), `/start` & `/help`.
- **Phase 2:** Multi-step trip planning wizard (`/newtrip`), station geocoding via OpenStreetMap, trip lifecycle (`/mytrip`, `/endtrip`, `/canceltrip`).
- **Phase 3:** Live GPS tracking via Haversine distance, speed smoothing, dynamic ETA calculation, proximity alerts (`ETA <= 4m` or `d <= 1km`), auto-arrival completion (`d <= 150m`), and overshoot detection.
- **Phase 4 (New):** AI Travel Guide powered by **Groq** (`llama-3.3-70b-versatile`) and **DuckDuckGo search**:
  - One-time interactive onboarding questionnaire (`/preferences` or on first `/explore`).
  - Stored preferences: interests, budget level (`low`, `medium`, `high`), and pace (`relaxed`, `packed`).
  - Active Trip context auto-injection: automatically applies the destination of your ongoing trip if not specified in your query.
  - Strictly grounded prompts: prevents hallucinated prices or hours by relying on real-time search snippets.
- **Phase 5 (New): Smart Tourist Safety Monitoring & Incident Response** — AI + Geo-Fencing + Blockchain Digital ID (see below).

---

## 🛡️ Phase 5 — Smart Tourist Safety Monitoring & Incident Response

### 1. Blockchain-based Digital Tourist ID
| Command | What it does |
|---|---|
| `/registerid` | 6-step wizard → issues `TID-XXXXXXXXXX`, valid for the trip duration |
| `/myid` | Shows your ID card + live verification result |
| `/verifyid TID-…` | Anyone (police, hotel, guide) can verify an ID |
| `/revokeid` | Revokes your ID (recorded on-chain) |
| `/ledger` | Shows the chain, recent blocks and integrity status |

- **Hash-chained ledger with proof-of-work**: each block stores the SHA-256 of the previous block, so editing any old block breaks the chain.
- **Privacy by design**: the document number is never stored, only a salted hash and the last 4 characters. The bot deletes the message containing the number from the chat. Ledger blocks contain no personal data.
- **Tamper detection**: verification recomputes the identity hash from the database and compares it with the one sealed on-chain.
- SOS events, incidents and resolutions are also written to the ledger as an immutable audit trail.

### 2. Geo-Fencing
- Circular risk zones: 🟢 safe · 🟡 caution · 🟠 high · 🔴 restricted. Five **demo zones** are seeded (Kaziranga, Nathu La, Cherrapunji, Tawang, Jaipur old city). The safety desk adds real ones with `/addzone`.
- Every location ping is checked for **APPROACH** (1 km before a high or restricted zone), **ENTER** and **EXIT**.
- Entering a restricted zone creates an incident and alerts guardians and the safety desk. Entering a high-risk zone alerts guardians.
- `/zones` lists risk zones near you. `/nearby` finds the closest police, hospitals, pharmacies and fire stations (OpenStreetMap Overpass) and the local emergency numbers.

### 3. AI Monitoring & Anomaly Detection (during trips)
| Detection | Trigger |
|---|---|
| Abrupt halt | Moving ≥ 50 km/h, then stationary for 3+ min away from the destination (possible accident) |
| Prolonged stop | Stationary 20+ min mid-journey |
| Route deviation | Distance to destination grows 2+ km over consecutive updates |
| Signal lost | No location for `SAFETY_INACTIVITY_MIN` during an active trip |
| Safety timer | `/timer 90`: check-in prompt if you haven't replied with `/safe` |

Each detection sends a **check-in** with ✅ *I'm safe* / 🆘 *I need help* buttons. If you don't reply within `SAFETY_ESCALATE_MIN`, an incident is created automatically and guardians and the safety desk are alerted.
- **Safety score** (`/safety`, 0–100): combines zone risk, local time of night, open incidents, recent anomalies, pending check-ins and preparedness (ID and guardian).
- **AI incident triage**: Groq classifies free-text reports by category and severity and gives first-response advice. If no API key is set, a keyword model takes over. The AI result can never be less severe than the keyword model's.
- **AI safety briefing** (`/safetytips Shillong`): web-grounded advice on scams, risky areas and emergency numbers.

### 4. Incident Response
| Command | What it does |
|---|---|
| `/sos` or 🆘 button | Critical alert with your location to guardians and the safety desk, plus emergency numbers and the nearest police/hospital |
| `/report` | Guided report (type → description → location) with AI triage |
| `/incidents`, `/resolve ID` | Track and close incidents |
| `/efir ID` | Generates a draft police complaint (E-FIR) `.txt` with movement trail and ledger proof |
| `/safe` | Answers check-ins, cancels the timer and closes auto/SOS incidents (responders are told you're safe) |

**Guardians**: `/addguardian Ravi` creates a single-use invite link (`t.me/<bot>?start=g_…`). When the guardian opens it, they receive your alerts, can acknowledge or resolve them with buttons, and can see your last location with `/whereis`.

**Safety desk (authorities)**: add Telegram IDs to `ADMIN_IDS` in `.env`. Commands: `/dashboard` (live counts, priority queue, tourists in risk zones, ledger health), `/incidents all`, `/whereis TID-…`, `/addzone`, `/removezone`.

A persistent keyboard (🆘 SOS · 🛡️ Safety status · 🏥 Nearby help · 🪪 My Tourist ID) works at any time, even in the middle of another conversation.

### Demo walkthrough
1. `/registerid` → `/addguardian Mum` → open the link from a second Telegram account.
2. `/newtrip` to any destination, then share a location pin near `26.45, 93.17`. You'll get a restricted-zone alert (Kaziranga), and the guardian receives the alert and a map pin.
3. Tap 🆘 SOS. The guardian taps 👀 Acknowledge, and you get notified.
4. `/report` → Theft → "bag snatched at the station" → `/efir <id>`.
5. Put your own ID in `ADMIN_IDS` and run `/dashboard`.

### Tests
```powershell
.\venv\Scripts\python test_safety.py
```
This covers ledger tamper detection, the ID lifecycle, geo-fence events, anomaly detection, the safety score, triage, SOS fan-out and check-in escalation, using a temporary database.

---

## 🚀 Phase 6 — Next-Gen Safety Intelligence

| Feature | How to use | What happens |
|---|---|---|
| 🤖 **AI Safety Copilot** | `/ask …` or just **type any message** | Answers using your live situation: place, zone, safety score, weather, nearby hotspots and trip. Replies in your language. Distress wording shows an instant SOS button. |
| 🎙️ **Voice SOS** | Send a **voice note** in any language | Groq Whisper transcribes it. Distress words ("help", "bachao", "बचाओ", "au secours", "助けて"…) trigger an SOS automatically: location, recording forwarded to responders, and the recording's fingerprint sealed on the ledger. Other voice notes go to the copilot. |
| 📸 **Evidence vault** | Send a **photo** (with a caption for new incidents) | SHA-256 fingerprint sealed as an `EVIDENCE` block. The photo is attached to your latest open incident (or opens a new one via AI triage), forwarded to responders and listed in the `/efir` draft. |
| 🔥 **Self-learning hotspots** | `/hotspots` (automatic warnings) | Incidents from the last 30 days are clustered (DBSCAN-style, 500 m) into dynamic danger zones, weighted by recency and severity. You're warned when walking into one. They're shown on the command-center map and count towards your safety score. |
| 🌩️ **Weather & disaster watch** | `/weather` (automatic alerts) | Open-Meteo (free, no key) for now and the next 6 hours: thunderstorms, heavy rain or snow (landslide and flood risk), fog, extreme heat or cold, strong winds. The monitor pushes severe alerts to recently active travellers, once per hazard per day. |
| 🧭 **Safe-route analysis** | `/saferoute Amber Fort, Jaipur` | Real road route (OSRM, falls back to a straight line), sampled every 250 m against geo-fences and hotspots, plus destination weather and time of night. Gives a score, a verdict and the reasons, with directions and a one-tap safety timer for the trip. |
| 📢 **Community safety network** | `/community on\|off` | When someone reports theft, harassment, an accident or a hazard, travellers seen within 2 km get an anonymous heads-up (no name, only an approximate distance). |
| 🗣️ **Emergency phrasebook** | `/phrases`, `/phrases tamil`, `/phrases assamese where is the police?` | Offline phrases in Hindi, Bengali, Tamil, Spanish, French and Japanese (picked automatically from your location's country). AI translates custom phrases into any language. |
| 📱 **QR Digital ID** | `/myid`, `/registerid` | A QR code that opens the public verification page. Police and hotels can verify the ID against the blockchain from any phone. |
| 🖥️ **Web Command Center** | `/web` (admins get the private link) | Live dark-mode map with geo-fences, pulsing learned hotspots, incidents by severity and tourists (red = pending check-in). Also shows stats tiles, the incident queue (click to fly to it) and ledger health. Refreshes every 10 s. |
| 🌐 **Public portal** | `http://localhost:8080` | ID verification page (`/verify/TID-…`) and blockchain ledger explorer (`/ledger`). No personal data beyond the verified ID card. |

The web portal is built into the bot and needs no extra install. It starts with `python main.py` on `WEB_PORT` (default 8080). To make the QR codes work from phones, expose it (for example with `ngrok http 8080`) and set `PUBLIC_BASE_URL`. The safety score now also counts severe weather and nearby hotspots.

```powershell
.\venv\Scripts\python test_intel.py    # hotspots, weather, routes, distress, community, evidence, QR, web portal
```

---

## 📁 Project Structure

```
Travel Buddy/
├── main.py                # Bot entry point & ConversationHandler dispatcher
├── handlers.py            # Commands: /start, /help, /newtrip, /mytrip, /explore, /preferences
├── ai_guide.py            # DuckDuckGo search + Groq LLM integration
├── tracker.py             # Haversine distance, speed, ETA, and proximity alerts
├── database.py            # SQLite async layer (users, trips, user_preferences)
├── geocoding.py           # OpenStreetMap Nominatim geocoding service
├── safety_handlers.py     # Phase 5: SOS, report, ID, guardians, zones, dashboard commands
├── incident_service.py    # Incidents, responder alerts, check-ins, background monitor
├── blockchain_id.py       # Hash-chained PoW ledger + Digital Tourist ID
├── geofence.py            # Risk zones, nearby police/hospitals, emergency numbers
├── safety_monitor.py      # Anomaly detection + safety score
├── ai_safety.py           # AI incident triage + safety briefings
├── safety_db.py           # Safety tables (ids, ledger, incidents, zones, guardians…)
├── keyboards.py           # Persistent SOS / safety keyboard
├── test_safety.py         # Phase 5 test suite
├── intel_handlers.py      # Phase 6: copilot, voice SOS, evidence, /saferoute, /weather, /hotspots, QR, /web
├── risk_intel.py          # Hotspot clustering, weather hazards, route risk scoring
├── ai_copilot.py          # Copilot, Whisper voice, multilingual distress detection, phrasebook
├── web_dashboard.py       # Web command center + public ID verification & ledger explorer
├── test_intel.py          # Phase 6 test suite
├── test_phase4.py         # AI guide & preferences automated test suite
├── simulate_live_trip.py  # Live location tracking & alert simulator
├── test_phase2.py         # DB lifecycle & state transitions test suite
├── requirements.txt       # Dependencies
├── .env                   # BOT_TOKEN and GROQ_API_KEY
├── .env.example           # Example environment template
└── travel_bot.db          # Auto-created SQLite database
```

---

## 🗃️ Database Schema

### 1. `users` Table
| Column | Type | Description |
|---|---|---|
| `telegram_id` | INTEGER | Primary key |
| `username` | TEXT | Telegram username / display name |
| `current_trip_status` | TEXT | `'no_active_trip'`, `'planning'`, `'active'` |
| `created_at` | TEXT | ISO UTC timestamp |

### 2. `trips` Table
| Column | Type | Description |
|---|---|---|
| `trip_id` | INTEGER | Primary key (AUTOINCREMENT) |
| `user_id` | INTEGER | Foreign key to `users(telegram_id)` |
| `destination_name` | TEXT | Destination name (e.g. `Jaipur`, `Zurich HB`) |
| `destination_lat` | REAL | Latitude |
| `destination_lng` | REAL | Longitude |
| `transport_mode` | TEXT | `'train'` or `'bus'` |
| `status` | TEXT | `'planning'`, `'active'`, `'completed'`, `'cancelled'` |
| `alert_sent` | INTEGER | `0` or `1` (prevents duplicate alerts) |
| `created_at` | TEXT | ISO UTC timestamp |
| `completed_at` | TEXT | ISO UTC timestamp |

### 3. `user_preferences` Table
| Column | Type | Description |
|---|---|---|
| `telegram_id` | INTEGER | Primary key, Foreign key to `users(telegram_id)` |
| `interests` | TEXT | Stored interests (e.g. `food, history, nature`) |
| `budget_level` | TEXT | `'low'`, `'medium'`, `'high'` |
| `pace` | TEXT | `'relaxed'`, `'packed'` |
| `updated_at` | TEXT | ISO UTC timestamp |

---

## 🤖 AI Travel Guide System & User Prompts

### System Prompt
```text
You are Travel Buddy, an expert, thoughtful, and highly practical local travel concierge.
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
```

### User Prompt Template
```text
TRAVELER CONTEXT:
• Destination Context: {destination_context}
• Traveler Interests: {interests}
• Budget Level: {budget}
• Travel Pace: {pace}

TRAVELER QUESTION:
"{user_query}"

RECENT WEB SEARCH RESULTS:
{search_snippets}

Please provide a personalized, practical recommendation answering the traveler's question based on their preferences and the fresh search results.
```

---

## 🧪 How to Test End-to-End

### 1. Run the Automated Test Suites
Run the automated test for Phase 4:
```powershell
.\venv\Scripts\python test_phase4.py
```
*Output:*
```text
====================================================================
>>> RUNNING PHASE 4: AI TRAVEL GUIDE & PREFERENCES TESTS
====================================================================
[OK] Initial preferences check passed: None
[OK] Preferences insertion check passed
[OK] Preferences update (ON CONFLICT) check passed

Testing DuckDuckGo search integration for 'Jaipur street food'...
[OK] DuckDuckGo search passed: retrieved 3 fresh results
[OK] Prompt template formatting verified
[OK] Missing/Placeholder API key safety check passed

====================================================================
ALL PHASE 4 TESTS COMPLETED SUCCESSFULLY!
====================================================================
```

### 2. Configure Your Groq API Key
1. Get a free API key at [console.groq.com](https://console.groq.com/).
2. Add it to your `.env` file:
   ```
   GROQ_API_KEY=gsk_your_actual_groq_key
   ```

### 3. Start the Bot
```powershell
.\venv\Scripts\Activate.ps1
python main.py
```

### 4. Interactive Testing in Telegram

| Step | Action | Expected Bot Response |
|---|---|---|
| **1. First-Time /explore** | Send `/explore What should I see in Jaipur?` | Bot notices you haven't set preferences and asks: *"Question 1/3: What are your primary interests?"* with quick-reply buttons. |
| **2. Complete Preferences** | Choose Interests (e.g. 🍲 Food & Dining), Budget (💰 Low), and Pace (☕ Relaxed) | Bot saves preferences to `user_preferences` table and **automatically answers your pending question** with tailored street food spots and sights! |
| **3. Active Trip Context** | 1. Create a trip with `/newtrip` (Destination: `Zurich HB`)<br>2. Send `/explore good coffee shops` (without naming a city) | Bot automatically uses `Zurich HB` as context: *"📍 Guide for Zurich HB (Active Trip Context)"* and recommends cafes in Zurich suited to your budget and pace! |
| **4. Update Preferences** | Send `/preferences` anytime | Opens the questionnaire to update your interests, budget, or pace. |
