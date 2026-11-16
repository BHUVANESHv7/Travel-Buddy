# Deployment guide

Travel Buddy runs as **one long-lived process**: the Telegram bot (long polling), the safety monitor
loop and the web portal share an event loop and one SQLite file. There is no webhook, no separate
database server and no build step.

What the host needs:

| Need | Why |
|---|---|
| Always on (no "sleep when idle") | The bot polls Telegram and the monitor checks tourists every minute. Free hosts that sleep idle apps will miss SOS alerts. |
| A writable disk that survives restarts | `travel_bot.db` holds the ledger, IDs, incidents and zones. |
| Outbound HTTPS | Telegram, Groq, Open-Meteo, OSRM, OpenStreetMap. |
| Inbound HTTPS *(only for the web portal)* | Phones open QR codes and magic sign-in links; browsers only share location over HTTPS. |

About 100 MB of RAM is enough.

---

## 1. Before you deploy (checklist)

1. **Create the config:** `cp .env.example .env`, then set:
   - `BOT_TOKEN`, from @BotFather
   - `ADMIN_IDS`: your safety-desk Telegram IDs (send `/dashboard` to the bot to see yours)
   - `PUBLIC_BASE_URL=https://your-public-address`
   - `TRUST_PROXY=1` when the portal sits behind a proxy or tunnel
   - `WEB_PORT_FALLBACK=0` on servers
   - `GROQ_API_KEY` *(optional)*: free key; without it the AI runs in offline mode
2. **Keep `SEED_STARTER_ZONES=0`.** Production starts with no sample data. Add your real risk zones after launch with `/addzone` or the **Desk** tab of the command center.
3. **Run the preflight check.** It validates the config and database, tests the token against Telegram and checks the web port:
   ```bash
   python scripts/preflight.py
   ```
   It must end with `READY`. Warnings are allowed but worth reading.
4. **Run the tests.**
   - Offline suites: `python scripts/run_tests.py`
   - Optional live check through the real Telegram API: `python tests/live_check.py`. It uses a temporary database.
5. **Stop any other copy of the bot.** Telegram allows only one polling process per token. A second copy causes `Conflict: terminated by other getUpdates request`.

---

## 2. Option A: Docker (recommended)

```bash
docker compose up -d --build
docker compose logs -f
```

- **Storage:** the database and logs live in `./data` on the host (mounted at `/data`), so rebuilding the container never loses data.
- **Network:** the portal is published on `127.0.0.1:8080` only. Expose it through HTTPS (section 4).
- **Health:** Docker's `HEALTHCHECK` calls `/healthz`. Use `docker ps` to see `healthy`.
- **Restarts:** the container runs as an unprivileged user and restarts automatically after crashes or reboots (`restart: unless-stopped`).

To update:

```bash
git pull && docker compose up -d --build
```

---

## 3. Option B: Linux server with systemd

```bash
sudo useradd --system --home /opt/travel-buddy travelbuddy
sudo git clone https://github.com/BHUVANESHv7/Travel-Buddy.git /opt/travel-buddy
cd /opt/travel-buddy
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env          # fill it in (section 1)
sudo chown -R travelbuddy:travelbuddy /opt/travel-buddy
sudo cp deploy/travel-buddy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now travel-buddy
journalctl -u travel-buddy -f
```

- **Preflight:** the unit runs `scripts/preflight.py --offline` before every start, so a broken `.env` fails fast with a clear message.
- **Restarts:** the service restarts automatically after a crash.

---

## 4. HTTPS for the web portal (free)

Pick one:

- **Caddy on a server with a domain.** Point the domain's DNS at the server, edit the domain in [`deploy/Caddyfile`](../deploy/Caddyfile), then run Caddy. It gets and renews a Let's Encrypt certificate automatically.
- **Cloudflare Tunnel.** Needs no open ports and no public IP, and also works from a home PC.
  - **Quick test:** `cloudflared tunnel --url http://localhost:8080` prints a temporary `https://….trycloudflare.com` address. It changes on every restart, so it suits testing only.
  - **Permanent address:** create a *named* tunnel on your own domain in the Cloudflare dashboard.

Then set in `.env`, and restart:

```
PUBLIC_BASE_URL=https://safety.your-domain.org
TRUST_PROXY=1
```

With an `https://` base URL, session cookies are marked `Secure` and HSTS is sent.

> Only enable `TRUST_PROXY=1` when the portal is reachable **only** through the proxy (it binds to
> `127.0.0.1` by default). Otherwise a visitor could fake their IP to dodge rate limits.

---

## 5. After launch

| Task | How |
|---|---|
| Add real risk zones | `/addzone <risk> <radius_m> <lat> <lng> <name> \| <advisory>` or the command center **Desk** tab (click the map) |
| Open the command center | Sign in at `/login` with an admin account, or use the private link the bot sends admins at start-up |
| Check health | `curl https://your-address/healthz` → `{"ok": true, ...}`; point a free uptime monitor at it |
| Watch free-tier usage | `/usage` in Telegram or the Desk tab |
| Back up the database | `python scripts/backup_db.py` (safe while running; keeps the newest 14). Daily cron: `0 3 * * * cd /opt/travel-buddy && venv/bin/python scripts/backup_db.py` |
| Restore a backup | Stop the bot, copy the backup over `travel_bot.db`, delete any `travel_bot.db-wal` / `-shm`, start again |
| Rotate the command-center secret | Set a new `DASHBOARD_TOKEN`, or delete the `dashboard_token` row from `app_settings`, then restart |
| Logs | `travel_buddy.log` rotates at 2 MB (5 files kept); `LOG_LEVEL=DEBUG` for detail |

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Conflict: terminated by other getUpdates request` | Another copy of the bot is running with the same token. Stop it. |
| Bot exits at start with `Config:` errors | Fix the listed `.env` values; `python scripts/preflight.py` explains each one. |
| `/healthz` returns 503 | `bot: false` means the Telegram side didn't start (check the token and network); `database: false` means the DB path isn't writable. |
| Web portal "not started" in the log | The port is taken. Free it or change `WEB_PORT`; with `WEB_PORT_FALLBACK=0` the bot won't silently move ports. |
| QR codes / sign-in links open `localhost` on phones | `PUBLIC_BASE_URL` is still local. Set your HTTPS address. |
| "Map server failed" warnings | A public OpenStreetMap server is busy; the bot switches to a mirror or Nominatim automatically. |
