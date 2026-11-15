# 📁 Travel Buddy — Project Folder Structure (Phase 4: AI Travel Guide)

```
Travel Buddy/
│
├── ⚙️ Configuration & Environment
│   ├── .env                    # Secret configuration (BOT_TOKEN, GROQ_API_KEY)
│   ├── .env.example            # Sample environment variables reference
│   ├── .gitignore              # Files and folders excluded from git tracking
│   └── requirements.txt        # Pinned Python dependencies (PTB, Groq, DDGS, aiosqlite)
│
├── 🧠 Core Application Code
│   ├── main.py                 # Bot entry point & lifecycle manager
│   │                           # - Loads environment (.env)
│   │                           # - Inits SQLite tables (users, trips, user_preferences)
│   │                           # - Registers ConversationHandlers (/newtrip, /preferences)
│   │                           # - Registers commands (/start, /help, /mytrip, /explore, etc.)
│   │                           # - Starts long-polling for messages & location updates
│   │
│   ├── handlers.py             # Telegram commands & conversation handlers
│   │                           # - /start, /help
│   │                           # - /newtrip (multi-step ConversationHandler)
│   │                           # - /mytrip, /endtrip, /canceltrip
│   │                           # - /preferences (onboarding flow for interests, budget, pace)
│   │                           # - /explore (AI travel guide query handler)
│   │                           # - live_location_handler (GPS tracker & proximity alerts)
│   │
│   ├── ai_guide.py             # AI Travel Concierge Engine
│   │                           # - DuckDuckGo web search integration (via ddgs / thread pool)
│   │                           # - Stored user preferences & journey context injector
│   │                           # - System & user prompt synthesis
│   │                           # - AsyncGroq LLM caller (llama-3.3-70b-versatile)
│   │
│   ├── tracker.py              # Live Location & Proximity Alerting Engine
│   │                           # - Haversine distance formula
│   │                           # - Sliding window speed estimation (3-5 pings)
│   │                           # - Dynamic ETA calculation
│   │                           # - Proximity alert evaluation (ETA <= 4m or d <= 1km)
│   │                           # - Auto-arrival detection (d <= 150m)
│   │                           # - Destination overshoot detection
│   │
│   ├── database.py             # SQLite database layer (aiosqlite)
│   │                           # - users table (telegram_id, username, current_trip_status)
│   │                           # - trips table (trip_id, user_id, destination, coordinates, status)
│   │                           # - user_preferences table (telegram_id, interests, budget, pace)
│   │                           # - CRUD helpers & auto-migrations
│   │
│   ├── geocoding.py            # Geocoding service
│   │                           # - OpenStreetMap Nominatim integration
│   │                           # - Converts address / station text to lat/lng
│   │
│   ├── 🛡️ Phase 5 — Smart Tourist Safety
│   ├── safety_handlers.py      # /sos /report /registerid /myid /verifyid /addguardian /safety
│   │                           # /nearby /zones /timer /safe /efir /dashboard /addzone /whereis
│   ├── incident_service.py     # Incident creation, guardian/authority alerts, check-ins,
│   │                           # background monitor (lost signal, timers, escalation)
│   ├── blockchain_id.py        # SHA-256 hash-chained proof-of-work ledger + Digital Tourist ID
│   ├── geofence.py             # Risk zones, enter/exit/approach events, Overpass emergency
│   │                           # services lookup, country emergency numbers
│   ├── safety_monitor.py       # Abrupt halt / prolonged stop / route deviation + safety score
│   ├── ai_safety.py            # Groq incident triage (rule-based fallback) + safety briefings
│   ├── safety_db.py            # tourist_ids, ledger, guardians, incidents, geofence_zones,
│   │                           # safety_state, location_history tables
│   ├── keyboards.py            # Persistent 🆘 SOS / safety quick-access keyboard
│   │
│   ├── 🚀 Phase 6 — Next-Gen Intelligence
│   ├── intel_handlers.py       # AI copilot (/ask + free text), voice SOS, photo evidence vault,
│   │                           # /saferoute /weather /hotspots /phrases /community /web, QR IDs
│   ├── risk_intel.py           # Self-learning hotspots (density clustering), Open-Meteo hazards,
│   │                           # OSRM route risk analysis
│   ├── ai_copilot.py           # Context-aware copilot, Whisper transcription, multilingual
│   │                           # distress detection, emergency phrasebook & AI translation
│   └── web_dashboard.py        # Web command center (live Leaflet map), public ID verification,
│                               # blockchain ledger explorer — Python stdlib HTTP server
│
├── 🧪 Testing & Simulation
│   ├── test_safety.py          # Phase 5 safety layer test suite (temp database)
│   ├── test_intel.py           # Phase 6 intelligence test suite (temp database)
│   ├── test_phase4.py          # AI Guide & Preferences verification suite
│   ├── simulate_live_trip.py   # Live GPS route simulation & proximity alert verification
│   └── test_phase2.py          # Database lifecycle and trip state transition verification
│
├── 🗄️ Runtime Data & Logs (Generated)
│   ├── travel_bot.db           # SQLite database storing users, trips & preferences
│   └── travel_buddy.log        # Application runtime log file
│
└── 📦 Environment
    └── venv/                   # Python 3.12 virtual environment
```
