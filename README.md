<div align="center">

# 🛡️ Travel Buddy

### Smart Tourist Safety Monitoring & Incident Response System
**AI · Geo-Fencing · Blockchain-based Digital ID**

A Telegram travel companion that keeps tourists safe in real time. It tracks journeys, warns about risky areas, detects emergencies automatically and gets help to them fast. Authorities get a live command center.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-21.6-26A5E4?logo=telegram&logoColor=white)](https://python-telegram-bot.org/)
[![Groq](https://img.shields.io/badge/AI-Groq%20Llama%203.3%20%2B%20Whisper-F55036)](https://console.groq.com/)
[![OpenStreetMap](https://img.shields.io/badge/Maps-OpenStreetMap-7EBC6F?logo=openstreetmap&logoColor=white)](https://www.openstreetmap.org/)
[![SQLite](https://img.shields.io/badge/DB-SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)

[Features](#-features) · [Quick start](#-quick-start) · [Commands](#-command-reference) · [Architecture](docs/ARCHITECTURE.md) · [Testing](#-testing)

<img src="docs/assets/command-center.jpg" alt="Tourist Safety Command Center — live map of tourists, geo-fences, hotspots and incidents" width="820">

<sub><i>Web Command Center: live tourists, geo-fences, learned hotspots and a prioritised incident queue (demo data).</i></sub>

</div>

---

## 📑 Table of contents

- [Overview](#-overview)
- [Features](#-features)
- [How it works](#-how-it-works)
- [Tech stack](#-tech-stack)
- [Project structure](#-project-structure)
- [Quick start](#-quick-start)
- [Configuration](#%EF%B8%8F-configuration)
- [Command reference](#-command-reference)
- [Web portal](#-web-portal)
- [Testing](#-testing)
- [Security & privacy](#-security--privacy)
- [Roadmap](#%EF%B8%8F-roadmap)
- [Contributing](#-contributing)
- [Acknowledgements](#-acknowledgements)

---

## 🌏 Overview

Tourists in unfamiliar places, especially remote, border or hazard-prone regions, face risks that existing tools handle poorly. Help arrives late because nobody knows where they are, families can't see that something is wrong, and authorities have no verified identity or live picture.

**Travel Buddy** works inside Telegram, so there is no app to install. It combines:

| Pillar | What it delivers |
|---|---|
| 🪪 **Blockchain Digital ID** | A tamper-evident tourist ID with QR verification that police, hotels and guides can check instantly. |
| 🗺️ **Geo-fencing** | Alerts on approaching, entering and leaving risk zones, plus danger zones learned automatically from incident reports. |
| 🤖 **AI monitoring** | Detects sudden halts, long stops, route deviation and lost signal, with check-ins that escalate when unanswered. |
| 🚨 **Incident response** | One-tap or voice SOS, AI-triaged reports, a sealed evidence vault, guardian and authority alerts, and E-FIR drafts. |

---

## ✨ Features

<details open>
<summary><b>🪪 Blockchain-based Digital Tourist ID</b></summary>

- 6-step registration that issues a `TID-XXXXXXXXXX` ID, valid for the length of the trip
- Each ID is sealed in a **SHA-256 hash-chained, proof-of-work ledger**. Editing any record or block is detected.
- **QR code** opens a public verification page; `/verifyid` checks an ID from inside Telegram
- Privacy by design: the document number is **never stored** (only a salted hash), and the message containing it is deleted from the chat
- SOS events, incidents, resolutions and evidence are written to the same immutable audit trail

</details>

<details open>
<summary><b>🗺️ Geo-fencing & risk intelligence</b></summary>

- Risk zones (🟢 safe · 🟡 caution · 🟠 high · 🔴 restricted) with **approach** warnings 1 km before the edge, plus **enter** and **exit** alerts
- Entering a restricted zone creates an incident and notifies guardians and the safety desk
- **Self-learning hotspots:** incident reports are clustered (DBSCAN-style) into dynamic danger zones, weighted by recency and severity
- **Severe-weather watch** (Open-Meteo): thunderstorms, heavy rain (landslide and flood risk), fog, extreme heat or cold, strong winds
- **`/saferoute`:** the real road route is scored against zones, hotspots, weather and time of night, with reasons
- Nearest police, hospitals and pharmacies (OpenStreetMap) and country-specific emergency numbers

</details>

<details open>
<summary><b>🤖 AI monitoring & assistance</b></summary>

- **Anomaly detection** during trips: abrupt halt (possible accident), prolonged stop, route deviation, lost signal
- **Check-ins** with ✅ *I'm safe* / 🆘 *I need help* buttons. No answer means automatic escalation.
- **Safety score (0–100)** from zone risk, time of night, incidents, anomalies, weather, hotspots and preparedness
- **AI Safety Copilot:** just type a message. It answers using your live location, zone, weather and nearby hotspots, in your language.
- **AI incident triage** (category, severity and first-response advice). It can never rank an emergency lower than the rule-based classifier does.
- **AI safety briefings** for any destination, plus an emergency **phrasebook** (Hindi, Bengali, Tamil, Spanish, French, Japanese and AI translation)

</details>

<details open>
<summary><b>🚨 Incident response</b></summary>

- **SOS** by command, persistent 🆘 button or **voice note in any language** ("help", "bachao", "बचाओ", "au secours", "助けて"…)
- Location, Digital ID and emergency contact are sent to **guardians** and the **safety desk**, with Acknowledge / Resolve buttons
- **Evidence vault:** photos and voice notes are fingerprinted (SHA-256), sealed on the ledger and forwarded to responders
- **E-FIR draft** with movement trail, evidence hashes and ledger proof, ready to file with the police
- **Safety timer** (`/timer 90`) for hikes and nights out
- Anonymous **community alerts** for travellers within 2 km of a reported incident

</details>

<details>
<summary><b>🚆 Journey companion (core)</b></summary>

- Trip wizard with station geocoding; live GPS tracking with smoothed speed and ETA
- Stop alert when ETA ≤ 4 min or distance ≤ 1 km, auto-arrival at 150 m, overshoot detection
- Personalised AI travel guide (`/explore`) grounded in fresh web search results

</details>

---

## 🧠 How it works

```mermaid
flowchart LR
    T[🧳 Tourist] -- location · voice · photo · text --> BOT[Telegram bot]
    BOT --> TRK[Journey tracker]
    BOT --> GEO[Geo-fencing<br/>+ learned hotspots]
    BOT --> MON[Anomaly detection<br/>+ safety score]
    BOT --> AI[AI copilot · triage · Whisper]
    GEO & MON & AI --> INC{Incident?}
    INC -- yes --> LED[(⛓️ Ledger)]
    INC -- yes --> R[👨‍👩‍👧 Guardians<br/>👮 Safety desk]
    INC -- nearby --> C[📢 Community alert]
    R -. acknowledge / resolve .-> BOT
    LED --> WEB[🖥️ Command center<br/>🌐 ID verification]
```

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the package layout, sequence diagrams, data model, ledger design and every detection threshold.

---

## 🧰 Tech stack

| Layer | Technology |
|---|---|
| Bot framework | [python-telegram-bot](https://python-telegram-bot.org/) 21 (asyncio) |
| AI | [Groq](https://console.groq.com/): Llama 3.3 70B (guide, triage, copilot) and Whisper large-v3-turbo (voice) |
| Search grounding | DuckDuckGo (`ddgs`) |
| Maps & geodata | OpenStreetMap Nominatim, Overpass, OSRM routing, Leaflet |
| Weather | Open-Meteo |
| Storage | SQLite via `aiosqlite` |
| Ledger | SHA-256 hash chain with proof-of-work (built in) |
| Web portal | Python standard library HTTP server, Leaflet, hand-built CSS and JavaScript (no framework) |
| QR codes | `segno` |

---

## 📁 Project structure

```
Travel-Buddy/
├── main.py                     # Entry point: wires handlers, monitor loop and web portal
├── travel_buddy/
│   ├── core/                   # Persistence, GPS tracking, geocoding
│   │   ├── database.py
│   │   ├── tracker.py
│   │   └── geocoding.py
│   ├── ai/                     # Groq-powered intelligence
│   │   ├── travel_guide.py     #   /explore recommendations
│   │   ├── triage.py           #   incident classification & safety briefings
│   │   └── copilot.py          #   copilot, voice, distress detection, phrasebook
│   ├── safety/                 # Safety platform
│   │   ├── ledger.py           #   blockchain ledger & Digital Tourist ID
│   │   ├── geofence.py         #   zones, emergency services & numbers
│   │   ├── monitor.py          #   anomaly detection & safety score
│   │   ├── incidents.py        #   incidents, responders, check-ins, monitor loop
│   │   ├── risk_intel.py       #   hotspots, weather, safe routes
│   │   └── store.py            #   safety schema & queries
│   ├── bot/                    # Telegram interface
│   │   ├── handlers.py         #   start/help, trips, live location, AI guide
│   │   ├── safety_handlers.py  #   ID, guardians, SOS, reports, E-FIR, admin
│   │   ├── intel_handlers.py   #   copilot, voice, evidence, routes, weather, QR
│   │   └── keyboards.py
│   └── web/
│       ├── dashboard.py        # HTTP server, JSON API, data access
│       ├── pages.py            # HTML views (landing, verify, ledger, command center)
│       └── static/             # Design system CSS, page scripts, favicon
├── tests/                      # Offline & integration test suites
├── docs/
│   ├── ARCHITECTURE.md
│   └── assets/
├── requirements.txt
└── .env.example
```

---

## 🚀 Quick start

### Prerequisites

- Python **3.10+** (developed on 3.12)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- *(Optional)* A free [Groq API key](https://console.groq.com/) for the AI features. Without one, rule-based fallbacks are used.

### Installation

```bash
git clone https://github.com/BHUVANESHv7/Travel-Buddy.git
cd Travel-Buddy

python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### Configure and run

```bash
cp .env.example .env        # Windows: copy .env.example .env
# edit .env and set BOT_TOKEN (and GROQ_API_KEY, ADMIN_IDS)
python main.py
```

Open your bot in Telegram and send `/start`. The web portal starts at **http://localhost:8080**.

### Five-minute demo

1. `/registerid` → you get a Digital Tourist ID and a QR code.
2. `/addguardian Mum` → open the invite link from a second Telegram account.
3. `/newtrip`, then share a location near `26.45, 93.17`. You'll get a restricted-zone alert (Kaziranga), and the guardian is notified with a map pin.
4. Tap **🆘 SOS**. The guardian taps **👀 Acknowledge**, and you see that they're responding.
5. Send a photo captioned *"my bag was snatched"*. It's sealed as evidence. Then run `/efir <id>`.
6. Put your Telegram ID in `ADMIN_IDS`, restart, and open the command-center link the bot sends you.

---

## ⚙️ Configuration

All settings live in `.env` (see [`.env.example`](.env.example)).

| Variable | Required | Default | Description |
|---|:---:|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from @BotFather |
| `GROQ_API_KEY` | | — | Enables the AI guide, triage, copilot, briefings and voice transcription |
| `GROQ_MODEL` | | `llama-3.3-70b-versatile` | Groq chat model |
| `ADMIN_IDS` | | — | Comma-separated Telegram IDs of the safety desk (send `/dashboard` to see yours) |
| `SAFETY_INACTIVITY_MIN` | | `30` | Minutes without location during a trip before a check-in |
| `SAFETY_ESCALATE_MIN` | | `15` | Minutes to answer a check-in before responders are alerted |
| `WEB_ENABLED` | | `1` | Set to `0` to disable the web portal |
| `WEB_HOST` / `WEB_PORT` | | `127.0.0.1` / `8080` | Web portal bind address (if the port is busy, the next free port is used automatically and shown in the log) |
| `DASHBOARD_TOKEN` | | random | Secret for the command center (a random one is sent to admins at start-up if unset) |
| `PUBLIC_BASE_URL` | | `http://localhost:8080` | Public URL used in QR codes (e.g. an ngrok or server URL) |
| `DATABASE_PATH` | | `./travel_bot.db` | SQLite database location |

---

## 📖 Command reference

<details open>
<summary><b>🚨 Safety & incidents</b></summary>

| Command | Description |
|---|---|
| `/sos` · 🆘 button · voice note | Emergency alert with location to guardians and the safety desk |
| `/report` | Guided incident report with AI triage |
| `/safety` | Live safety score and status |
| `/nearby` | Closest police, hospitals and emergency numbers |
| `/zones` · `/hotspots` | Geo-fenced risk zones and learned incident hotspots nearby |
| `/weather` | Weather hazards now and for the next 6 hours |
| `/saferoute <place>` | Route safety analysis with directions |
| `/timer <min>` · `/safe` | Safety timer and "I'm okay" |
| `/incidents` · `/resolve <id>` · `/efir <id>` | Manage incidents and draft a police report |
| `/monitoring on\|off` · `/community on\|off` | Toggle anomaly detection and community alerts |

</details>

<details>
<summary><b>🪪 Identity, guardians & AI</b></summary>

| Command | Description |
|---|---|
| `/registerid` · `/myid` · `/verifyid <TID>` · `/revokeid` | Digital Tourist ID lifecycle |
| `/ledger` | Ledger status and recent blocks |
| `/addguardian <name>` · `/guardians` · `/removeguardian <id>` | Emergency contacts |
| `/whereis` | Guardians: last location of the people they guard |
| `/ask <question>` *(or just type)* | AI Safety Copilot |
| `/safetytips <place>` | AI safety briefing |
| `/phrases [language] [text]` | Emergency phrasebook and translation |
| 📸 photo with caption | Sealed evidence attached to an incident |

</details>

<details>
<summary><b>🚆 Journeys & travel guide</b></summary>

| Command | Description |
|---|---|
| `/newtrip` · `/mytrip` · `/endtrip` · `/canceltrip` | Plan and track a journey (share **Live Location** to start tracking) |
| `/explore <question>` · `/preferences` | Personalised AI travel recommendations |

</details>

<details>
<summary><b>👮 Safety desk (ADMIN_IDS)</b></summary>

| Command | Description |
|---|---|
| `/dashboard` | Live counts, priority queue, tourists in risk zones, ledger health |
| `/incidents all` | All open incidents |
| `/whereis <TID>` | Last known location of any tourist |
| `/addzone <risk> <radius_m> <lat> <lng> <name> \| <advisory>` · `/removezone <id>` | Manage geo-fences |
| `/web` | Command-center link |

</details>

---

## 🖥️ Web portal

| Route | Access | Purpose |
|---|---|---|
| `/` | Public | Portal landing page |
| `/verify/<TID>` | Public | Digital ID verification (QR code target) |
| `/ledger` | Public | Ledger explorer (hashes only) |
| `/command?token=…` | 🔒 Token | Live command-center map |
| `/api/overview?token=…` | 🔒 Token | JSON feed for the command center |

<p align="center">
  <img src="docs/assets/id-verification.jpg" alt="Animated Digital Tourist ID verification page" width="720">
</p>

**Interface highlights**

- **Command center:** count-up KPI tiles that flash when values change; pulsing map markers by severity; a flashing banner and red vignette for critical alerts; slide-in toasts and optional alert sounds for new incidents. Also has tabs for incidents, tourists, hotspots and activity, with search and severity filters, and fly-to-incident on click.
- **ID verification:** a holographic ID card that tilts with the pointer; verification checks that tick one by one; an animated VALID / INVALID stamp.
- **Ledger explorer:** an animated chain timeline, expandable block payloads and click-to-copy hashes.
- **Throughout:** dark "aurora" theme, glass panels, reveal-on-scroll, responsive down to phone width, keyboard focus styles, and full support for the *reduced motion* accessibility setting.

To let phones open QR links, expose the portal (for example `ngrok http 8080`) and set `PUBLIC_BASE_URL`.

---

## 🧪 Testing

```bash
python tests/test_safety.py         # ledger tamper detection, ID lifecycle, geo-fencing, anomalies, escalation
python tests/test_intel.py          # hotspots, weather, safe routes, distress detection, evidence, QR, web portal
python tests/test_trips.py          # trip lifecycle & state transitions
python tests/simulate_live_trip.py  # live GPS simulation: alerts, arrival, overshoot
python tests/test_ai_guide.py       # preferences + web search grounding (needs internet)
python tests/live_check.py          # LIVE end-to-end run through the real Telegram API (see below)
```

The safety and intelligence suites run fully offline against a temporary database. No test touches your real `travel_bot.db`.

**Live end-to-end check.** After pressing **Start** in your bot once, `tests/live_check.py` sends a scripted session through the bot's real handlers: 40 checks covering ID registration with QR, geo-fencing, live journey tracking and arrival, reports, E-FIR, SOS, admin dashboard and the AI guide. Every reply is delivered to your Telegram chat in real time, and every Bot API call is verified. It uses the first ID in `ADMIN_IDS`, or `--chat-id`.

---

## 🔐 Security & privacy

- **No raw ID numbers stored:** only a salted SHA-256 hash and the last 4 characters
- **Ledger holds no personal data:** blocks contain IDs, hashes, types and severities only
- **Tamper evidence:** chain re-verification plus identity-hash comparison against the database
- **Least exposure:** community alerts are anonymous with approximate distance; the command center needs a secret token; the portal binds to localhost by default
- **Secrets stay local:** `.env`, the database and logs are git-ignored

> ⚠️ Travel Buddy supports, but does not replace, official emergency services. In danger, always call the local emergency number (e.g. **112** in India).

---

## 🗺️ Roadmap

- [ ] Multilingual bot interface (Hindi, regional languages)
- [ ] Anchoring ledger block hashes to a public blockchain
- [ ] Offline SMS fallback for SOS in low-connectivity areas
- [ ] Wearable / panic-button integration
- [ ] Authority-side incident assignment and response-time analytics

---

## 🤝 Contributing

Contributions are welcome!

1. Fork the repository and create a feature branch: `git checkout -b feature/my-feature`
2. Keep changes focused and add or extend tests in `tests/`
3. Run the test suites
4. Open a pull request describing the change

---

## 🙏 Acknowledgements

[python-telegram-bot](https://python-telegram-bot.org/) · [Groq](https://groq.com/) · [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors · [Open-Meteo](https://open-meteo.com/) · [OSRM](https://project-osrm.org/) · [Leaflet](https://leafletjs.com/) · [segno](https://github.com/heuer/segno)

<div align="center">

Made with ❤️ for safer travel · **[⬆ back to top](#️-travel-buddy)**

</div>
