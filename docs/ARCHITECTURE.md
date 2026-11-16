# Architecture

This document describes how Travel Buddy is organised, how data flows through it, and the design decisions behind the safety features.

- [System overview](#system-overview)
- [Package layout](#package-layout)
- [Request flows](#request-flows)
- [Web portal & sign-in](#web-portal--sign-in)
- [Data model](#data-model)
- [Blockchain ledger design](#blockchain-ledger-design)
- [Detection and scoring rules](#detection-and-scoring-rules)
- [External services](#external-services)
- [Design decisions](#design-decisions)

---

## System overview

```mermaid
flowchart LR
    subgraph Users
        T[🧳 Tourist]
        G[👨‍👩‍👧 Guardian]
        A[👮 Safety desk]
    end

    subgraph Bot["Telegram bot (python-telegram-bot)"]
        H[bot/*_handlers]
    end

    subgraph Engine["travel_buddy"]
        CORE[core<br/>tracking · DB · geocoding]
        SAF[safety<br/>ledger · geofence · monitor<br/>incidents · risk intel]
        AI[ai<br/>guide · triage · copilot · voice]
        WEB[web<br/>command center · portal]
    end

    DB[(SQLite)]
    EXT[[Groq · OpenStreetMap · Open-Meteo · OSRM · DuckDuckGo]]

    T <--> H
    G <--> H
    A <--> H
    A -->|browser| WEB
    H --> CORE & SAF & AI
    SAF --> AI
    CORE & SAF --> DB
    WEB -->|read-only| DB
    AI & SAF & CORE --> EXT
    SAF -. background monitor .-> H
```

The bot and the web portal run in **one process**. The Telegram application runs on the asyncio event loop. A background task (`safety.incidents.monitor_loop`) checks every 60 s for lost signal, expired timers, unanswered check-ins and severe weather. The web portal is a standard-library `ThreadingHTTPServer` on a daemon thread that reads SQLite read-only.

## Package layout

```
travel_buddy/
├── core/
│   ├── database.py        # users, trips, user_preferences (aiosqlite)
│   ├── tracker.py         # Haversine, speed smoothing, ETA, arrival/overshoot detection
│   └── geocoding.py       # Nominatim forward geocoding
├── ai/
│   ├── travel_guide.py    # /explore — DuckDuckGo search + Groq, preference-aware
│   ├── triage.py          # incident classification (Groq JSON mode + rule-based floor), safety briefings
│   └── copilot.py         # context-aware copilot, Whisper transcription, distress detection, phrasebook
├── safety/
│   ├── store.py           # safety schema + queries (IDs, ledger, incidents, zones, guardians, evidence…)
│   ├── ledger.py          # hash-chained proof-of-work ledger + Digital Tourist ID issue/verify/revoke
│   ├── geofence.py        # zone enter/exit/approach, emergency numbers, Overpass service lookup
│   ├── monitor.py         # motion anomaly detection + safety score
│   ├── incidents.py       # raise_incident, responder fan-out, community alerts, check-ins, monitor loop
│   ├── risk_intel.py      # hotspot clustering, weather hazards, safe-route scoring
│   ├── forecast.py        # predictive risk model (spatio-temporal forecast + backtest)
│   └── actions.py         # channel-neutral actions shared by the bot and the website
├── bot/
│   ├── handlers.py        # /start /help, trip wizard, live location pipeline, /explore, /preferences
│   ├── safety_handlers.py # ID, guardians, SOS, reports, E-FIR, zones, dashboard, callbacks
│   ├── intel_handlers.py  # copilot, voice, photo evidence, /saferoute /weather /hotspots /phrases, QR
│   └── keyboards.py       # persistent SOS keyboard
└── web/
    ├── dashboard.py       # HTTP server: /command (token), /api/overview, /verify/<TID>, /ledger, /static/*
    ├── pages.py           # HTML views (server-rendered, all values escaped)
    ├── auth.py            # passwordless Telegram sign-in (codes, magic links), sessions
    ├── portal_api.py      # JSON API for tourists, guardians and officers
    ├── runtime.py         # runs web actions on the bot's asyncio loop
    └── static/            # portal.css · pages.css · command.css · portal.js · command.js · favicon.svg
```

**Dependency direction:** `bot → safety / ai / core`, `safety → ai / core`, `web → safety / core`. Nothing in `core` imports from the other packages. The two bot modules that need each other use local imports to avoid cycles.

## Request flows

### Live location update

```mermaid
sequenceDiagram
    participant T as Tourist
    participant H as bot.handlers
    participant TR as core.tracker
    participant S as bot.safety_handlers
    participant GF as safety.geofence
    participant M as safety.monitor
    participant RI as safety.risk_intel

    T->>H: location / live location edit
    H->>TR: process_location_ping (ETA, approach, arrival, overshoot)
    TR-->>H: event → stop alert / arrival message
    H->>S: safety_location_hook
    S->>S: update last seen, clear lost-signal check-in, store trail
    S->>GF: evaluate_geofence → APPROACH / ENTER / EXIT
    GF-->>S: events (restricted ENTER → incident + responders)
    S->>M: analyze_motion → ABRUPT_HALT / PROLONGED_STOP / ROUTE_DEVIATION
    M-->>S: anomalies → check-in with ✅ / 🆘 buttons
    S->>RI: hotspots_near → proactive hotspot warning
```

### SOS and escalation

```mermaid
sequenceDiagram
    participant T as Tourist
    participant B as Bot
    participant L as Ledger
    participant R as Guardians + safety desk
    participant N as Nearby travellers

    T->>B: /sos · 🆘 button · voice "help"/"bachao"
    B->>L: append SOS block (IDs + evidence hash only)
    B->>R: alert + live location pin + Acknowledge / Resolve buttons
    B-->>T: emergency numbers (by country) + nearest police/hospital
    R->>B: 👀 Acknowledge
    B-->>T: "<responder> is responding"
    Note over B,N: Non-SOS incidents (theft, harassment…) also send<br/>anonymous community alerts within 2 km
    Note over B: Unanswered check-in after SAFETY_ESCALATE_MIN<br/>→ automatic incident → responders
```

## Web portal & sign-in

```mermaid
sequenceDiagram
    participant B as Browser
    participant W as Web server (thread)
    participant R as runtime.run()
    participant A as safety.actions
    participant T as Telegram bot

    B->>W: POST /api/auth/request {@username}
    W->>T: send 6-digit code to that Telegram chat
    B->>W: POST /api/auth/verify {code}
    W-->>B: Set-Cookie tb_session (HttpOnly, SameSite=Lax)
    B->>W: POST /api/me/sos  (X-CSRF header)
    W->>R: run coroutine on the bot's event loop
    R->>A: trigger_sos(bot, uid)
    A->>T: alert guardians + safety desk (same path as the /sos command)
    W-->>B: incident, emergency numbers, nearest help
```

- **One implementation, two channels.** `safety/actions.py` holds the shared behaviour: location ingestion with geo-fences and hotspots, SOS, "I'm safe", acknowledge/resolve, E-FIR and guardian invites. The bot passes `message.reply_html` as `say`; the portal collects the notes and shows them in the browser.
- **Threads → event loop.** The HTTP server runs in threads, while Telegram I/O lives on the bot's asyncio loop. `web/runtime.py` schedules coroutines there with `run_coroutine_threadsafe`, so web alerts use the same bot connection.
- **Roles.** Tourist: own data. Guardian: people who linked them (`guardians` table). Officer: Telegram IDs in `ADMIN_IDS`, who can open `/command` when signed in. Every incident action re-checks `can_manage()`.
- **Security.** No passwords. Login codes (10 min, 5 attempts, 45 s resend limit) and magic links (15 min, single use) are stored as SHA-256 hashes. Sessions are 256-bit random tokens in HttpOnly SameSite=Lax cookies. Every state change needs `X-Requested-With`, which blocks cross-site form posts, plus a per-session `X-CSRF` token. Login answers are identical whether or not an account exists, login attempts are rate-limited per client, and responses send `X-Frame-Options: DENY`.

## Data model

| Table | Purpose |
|---|---|
| `users` | Telegram users and current trip status |
| `trips` | Journeys (destination, mode, status, alert flag) |
| `user_preferences` | AI guide personalisation (interests, budget, pace) |
| `tourist_ids` | Digital IDs: name, nationality, **salted hash** of the document number plus its last 4 characters, validity, identity hash, sealing block |
| `ledger` | Hash-chained blocks: `GENESIS`, `ID_ISSUED`, `ID_REVOKED`, `SOS`, `INCIDENT`, `INCIDENT_RESOLVED`, `EVIDENCE` |
| `guardians` | Single-use invite codes linked to guardian Telegram IDs |
| `incidents` | Type, severity, location, status lifecycle (`open → acknowledged → resolved`), source, AI advice, block hash |
| `evidence` | Photo / voice file IDs, SHA-256, transcript or caption, block hash |
| `geofence_zones` | Circular zones with risk level and advisory |
| `safety_state` | Per-user live state: last location, zones, pending check-in, timer, flags, alert de-duplication |
| `location_history` | Rolling 50-point trail per user (for E-FIR and search) |
| `web_sessions` · `web_login_codes` · `web_magic_links` | Portal sign-in (hashed tokens only) |
| `usage_counters` | Daily free-tier usage per service |

Schema changes are applied at startup as idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN` migrations.

## Blockchain ledger design

```
block_hash = SHA-256( index | timestamp | type | canonical_json(payload) | previous_hash | nonce )
valid      ⇔ block_hash starts with "000"  ∧  previous_hash == hash of block (index-1)
```

- **Tamper evidence:** changing any field of any block changes its hash, which breaks every later link. `verify_blocks()` recomputes the whole chain.
- **Identity binding:** the `ID_ISSUED` block seals `identity_hash = SHA-256(tourist_id, telegram_id, name, nationality, doc_type, doc_hash, validity)`. Verification recomputes it from the database row, so editing a stored ID is detected even when the chain itself is intact.
- **Privacy:** block payloads hold only IDs, hashes, types and severities. Names, phone numbers, locations and document numbers never enter the ledger.
- **Concurrency:** appends are serialised with an `asyncio.Lock`. Mining runs in a worker thread (`asyncio.to_thread`).

This is a single-node, permissioned ledger. It gives tamper evidence and an audit trail without the cost of a public chain. The `verify_blocks` / payload format is chain-agnostic, so block hashes could later be anchored to a public network.

## Detection and scoring rules

| Signal | Rule |
|---|---|
| Stop approaching | ETA ≤ 4 min or distance ≤ 1 km; arrival at ≤ 150 m; overshoot when moving away after closest approach |
| Geo-fence approach | ≤ 1 km from the edge of a `high` / `restricted` zone |
| Abrupt halt | Speed into stop ≥ 50 km/h, stationary within 100 m for ≥ 3 min, > 1.5 km from destination |
| Prolonged stop | Stationary ≥ 20 min mid-journey |
| Route deviation | 5 consecutive increasing distances to destination, total growth ≥ 2 km |
| Lost signal | Active trip, no location for `SAFETY_INACTIVITY_MIN` |
| Escalation | Check-in unanswered for `SAFETY_ESCALATE_MIN` → incident + responders |
| Hotspot | DBSCAN-style clustering, ε = 500 m, ≥ 2 incidents in 30 days; score = Σ severity × (0.3 + 0.7·e^(−age/10 d)) |
| Weather | Thunderstorm (WMO ≥ 95), heavy rain/snow, gusts ≥ 60 km/h, fog, feels-like ≥ 35/40 °C or ≤ −5 °C |
| Risk forecast | rate = Σ K(d; σ = 300 m) · [e^(−age/45 d)/45 + 0.3·e^(−age/3 d)/3] · P(hour | place); 180-day window; P(hour) shrunk to city profile (4 pseudo-counts) and tourist prior (12) |
| Forecast warning | Data-driven index ≥ 40 now or within 3 h, confidence ≥ medium; once per ~1 km cell per 6 h |

**Safety score** starts at 100 and subtracts points for each factor: zone risk (caution 15 / high 30 / restricted 40), late night (10), open incidents (20 each, max 2), recent anomalies (15 each, max 2), a pending check-in (20), no recent location (10), weather (5 / 15), a nearby hotspot or predicted incident risk (8 / 15, whichever is stronger), no digital ID (5) and no guardian (5).

**AI triage** never lowers severity below the rule-based classifier, so an outage or a model misjudgement cannot downgrade an emergency.

## External services

| Service | Used for | Key needed |
|---|---|---|
| Telegram Bot API | Messaging, files, location | `BOT_TOKEN` |
| Groq (Llama 3.3 70B, Whisper large-v3-turbo) | Guide, triage, copilot, briefings, voice | `GROQ_API_KEY` (optional; rule-based fallbacks) |
| OpenStreetMap Nominatim | Geocoding / reverse geocoding | No |
| OpenStreetMap Overpass | Nearby police, hospitals, pharmacies | No |
| Open-Meteo | Weather hazards | No |
| OSRM demo server | Road routes for `/saferoute` | No |
| DuckDuckGo | Grounding search for the guide and briefings | No |

Every network call has a timeout and degrades gracefully: features keep working with reduced detail when a service is unavailable.

## Design decisions

- **One process, no extra infrastructure.** SQLite plus a stdlib HTTP server keeps deployment to `python main.py`.
- **Privacy by default.** Hash-only ledger. Document numbers are never stored, and the message containing one is deleted from the chat. Community alerts are anonymous with only an approximate distance. The command center needs a secret token.
- **Fail safe, not silent.** Rule-based fallbacks exist for triage, distress detection and the copilot. Responders are only paged for medium-or-higher severity, while SOS always pages.
- **Testable cores.** Clustering, weather assessment, route scoring, the safety score and chain verification are pure functions. They are tested offline (`tests/test_intel.py`, `tests/test_safety.py`).
