"""
pages.py — HTML views for the web portal.

All dynamic values are HTML-escaped here. Styling and behaviour live in ./static
(portal.css + pages.css + portal.js for public pages, command.css + command.js for the
command center), served by dashboard.py under /static/.
"""

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"


def asset(name: str) -> str:
    """URL for a static file with a cache-busting version (file modification time)."""
    try:
        version = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"

SHIELD = ('<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" '
          'stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z"/>'
          '<path d="M9 12l2 2 4-4"/></svg>')
CHECK = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
CROSS = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>'

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800'
         '&family=JetBrains+Mono:wght@500;700;800&display=swap" rel="stylesheet">')


def _nav(active: str) -> str:
    def link(href, label, key, extra=""):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}{extra}>{label}</a>'
    return (f'<nav class="nav"><div class="container">'
            f'<a class="brand" href="/"><span class="brand-mark">{SHIELD}</span>Travel Buddy '
            f'<span class="muted" style="font-weight:500">Safety</span></a>'
            f'<div class="nav-links">{link("/#verify", "Verify ID", "verify")}'
            f'{link("/ledger", "Ledger", "ledger", " class=hide-sm")}{link("/#features", "Features", "features", " class=hide-sm")}'
            f'<a class="nav-cta" href="/login">My portal</a>'
            f'</div></div></nav>')


def _footer() -> str:
    year = datetime.now(timezone.utc).year
    return (f'<footer class="footer"><div class="container">'
            f'<span>© {year} Travel Buddy · Smart Tourist Safety Monitoring & Incident Response</span>'
            f'<span>Maps © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors</span>'
            f'</div></footer>')


def layout(title: str, body: str, *, active: str = "", body_class: str = "",
           head: str = "", scripts: str = "", chrome: bool = True, description: str = "") -> str:
    desc = escape(description or "Blockchain-verified tourist identity, AI incident response and live geo-fenced safety monitoring.")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{escape(title)} · Travel Buddy Safety</title>
<meta name="description" content="{desc}">
<meta name="theme-color" content="#05080f">
<meta name="color-scheme" content="dark">
<link rel="icon" href="{asset('favicon.svg')}" type="image/svg+xml">
{FONTS}
<link rel="stylesheet" href="{asset('portal.css')}">
{head}
</head>
<body class="{body_class}">
<div class="aurora" aria-hidden="true"><span></span><span></span><span></span></div>
{_nav(active) if chrome else ""}
<main>{body}</main>
{_footer() if chrome else ""}
<script src="{asset('portal.js')}" defer></script>
{scripts}
</body>
</html>"""


def pages_css() -> str:
    return f'<link rel="stylesheet" href="{asset("pages.css")}">'


# ─────────────────────────────────────────────
#  Landing
# ─────────────────────────────────────────────

FEATURES = [
    ("🪪", "Blockchain Digital ID", "Tamper-evident tourist IDs sealed in a proof-of-work hash chain, verifiable by QR in seconds."),
    ("🗺️", "Smart geo-fencing", "Approach, entry and exit alerts for risk zones, plus danger hotspots learned from real incidents."),
    ("🤖", "AI anomaly detection", "Spots sudden halts, long stops, route deviation and lost signal, then checks in automatically."),
    ("🆘", "One-tap & voice SOS", "Panic button or a voice note in any language alerts guardians and authorities with location."),
    ("📸", "Evidence vault", "Photos and recordings are fingerprinted on the ledger so they can't be altered later."),
    ("🌩️", "Hazard intelligence", "Severe-weather warnings and route safety scores that factor in zones, hotspots and night-time."),
]
STEPS = [
    ("Register", "Get a Digital Tourist ID and QR code in the Telegram bot, with no app to install."),
    ("Travel", "Share live location. Geo-fences, AI monitoring and weather watch run in the background."),
    ("Respond", "SOS, missed check-ins and restricted-zone entries reach guardians and the safety desk instantly."),
    ("Verify", "Police, hotels and guides scan the QR code to check the ID against the ledger."),
]


def landing_html(stats: dict, signed_in: bool = False) -> str:
    chain_ok = stats.get("chain_valid", True)
    feats = "".join(
        f'<div class="card hover feature"><div class="icon">{ic}</div><h3>{escape(t)}</h3><p>{escape(d)}</p></div>'
        for ic, t, d in FEATURES)
    steps = "".join(f'<div class="card step"><h3>{escape(t)}</h3><p>{escape(d)}</p></div>' for t, d in STEPS)
    blips = "".join(f'<span class="blip" style="left:{x}%;top:{y}%;--c:{c};--d:{d}s"></span>' for x, y, c, d in [
        (30, 28, "var(--ok)", 0.2), (68, 34, "var(--warn)", 0.9), (74, 66, "var(--crit)", 1.8),
        (36, 70, "var(--ok)", 2.6), (55, 18, "var(--accent)", 3.3)])
    body = f"""
<div class="container">
  <section class="hero">
    <div>
      <span class="eyebrow fade-up"><span class="dot live" style="color:var(--ok)"></span> Live safety network</span>
      <h1 class="fade-up d1">Travel anywhere.<br><span class="grad-text">We've got your back.</span></h1>
      <p class="lead fade-up d2">Tourist Safety Portal for Smart Tourist Safety Monitoring &amp; Incident Response:
        AI, geo-fencing and a blockchain-based Digital ID, working together in real time.</p>
      <div class="hero-actions fade-up d3">
        <a class="btn" href="/login">{'Open my portal' if signed_in else 'Sign in to my portal'}</a>
        <a class="btn ghost" href="#verify">Verify a Tourist ID</a>
      </div>
      <form id="verify" class="verify-box fade-up d4" onsubmit="location.href='/verify/'+encodeURIComponent(this.t.value.trim().toUpperCase());return false">
        <input class="input" name="t" placeholder="TID-XXXXXXXXXX" aria-label="Tourist ID" autocomplete="off" required pattern="[Tt][Ii][Dd]-[0-9A-Fa-f]{{10}}">
        <button class="btn" type="submit">Verify</button>
      </form>
      <div class="hint fade-up d5">Or scan the QR code on the tourist's Digital ID.</div>
    </div>
    <div class="radar fade-up d2" aria-hidden="true"><div class="sweep"></div>{blips}<div class="core">{SHIELD}</div></div>
  </section>

  <section class="stats" data-stagger>
    <div class="card stat"><b data-count="{stats['ids']}">0</b><span>Active Digital IDs</span></div>
    <div class="card stat"><b data-count="{stats['blocks']}">0</b><span>Ledger blocks sealed</span></div>
    <div class="card stat"><b data-count="{stats['zones']}">0</b><span>Geo-fenced risk zones</span></div>
    <div class="card stat"><b data-count="{stats['resolved']}">0</b><span>Incidents resolved</span></div>
  </section>
  <div class="reveal" style="margin-top:14px">
    <span class="badge {'ok' if chain_ok else 'bad'}"><span class="dot {'live' if chain_ok else ''}"></span>
      Ledger integrity {'verified' if chain_ok else 'BROKEN'}</span>
  </div>

  <section class="section" id="features">
    <div class="section-head reveal"><span class="eyebrow">Capabilities</span>
      <h2>One platform for prevention, detection and response</h2>
      <p>Everything works inside Telegram for tourists, with a live command center for authorities.</p></div>
    <div class="features" data-stagger>{feats}</div>
  </section>

  <section class="section">
    <div class="section-head reveal"><span class="eyebrow">How it works</span><h2>From check-in to rescue</h2></div>
    <div class="steps" data-stagger>{steps}</div>
  </section>

  <section class="card cta glow-border reveal">
    <h2>Are you on the safety desk?</h2>
    <p>Open the private command-center link the Travel Buddy bot sent you, or send <code>/web</code> in the bot.</p>
    <a class="btn ghost" href="/ledger">View public ledger</a>
  </section>
</div>"""
    return layout("Tourist Safety Portal", body, active="", head=pages_css())


# ─────────────────────────────────────────────
#  Verify
# ─────────────────────────────────────────────

def verify_html(res: dict) -> str:
    if not res["found"]:
        body = f"""
<div class="container notfound">
  <div class="big">🔍</div>
  <h1 class="fade-up">Tourist ID Not found</h1>
  <p class="muted fade-up d1"><code>{escape(res['tourist_id'])}</code> is not on record. Check the ID and try again.</p>
  <form class="verify-box fade-up d2" style="margin:28px auto 0" onsubmit="location.href='/verify/'+encodeURIComponent(this.t.value.trim().toUpperCase());return false">
    <input class="input" name="t" placeholder="TID-XXXXXXXXXX" aria-label="Tourist ID" required>
    <button class="btn" type="submit">Verify</button>
  </form>
</div>"""
        return layout("ID not found", body, active="verify", head=pages_css())

    r = res["record"]
    reasons = " ".join(res["reasons"]).lower()
    chain_ok = res["chain"]["valid"]
    checks = [
        ("Record found on the safety registry", f"Sealed in block #{res.get('block_index', '?')}", True),
        ("Ledger chain intact", f"{res['chain']['length']} blocks re-hashed with proof-of-work", chain_ok),
        ("Identity matches sealed hash", "Record unchanged since issue", "altered" not in reasons and "missing" not in reasons),
        ("ID not revoked", "Status: " + r["status"], r["status"] == "active"),
        ("Within validity period", f"Valid until {r['valid_until'][:10]}", "expired" not in reasons),
    ]
    steps = "".join(
        f'<div class="check-step {"pass" if ok else "fail"}"><div class="check-ico">{CHECK if ok else CROSS}</div>'
        f'<div><b>{escape(t)}</b><span>{escape(s)}</span></div></div>' for t, s, ok in checks)
    valid = res["valid"]
    verdict = ('<div class="verdict ok">VALID</div>' if valid else '<div class="verdict bad">INVALID</div>')
    problems = "".join(f"<li>{escape(x)}</li>" for x in res["reasons"])
    body = f"""
<div class="container {'invalid' if not valid else ''}">
  <div class="verify-wrap">
    <div>
      <div class="verify-title">
        <span class="eyebrow fade-up">Digital Tourist ID verification</span>
        <h1 class="fade-up d1">{'Identity verified ✓' if valid else 'Verification failed'}</h1>
      </div>
      <div class="id-card" aria-label="Digital Tourist ID card">
        <div class="id-top"><span class="brand"><span class="brand-mark">{SHIELD}</span>Travel Buddy ID</span><span class="chip"></span></div>
        {verdict}
        <div class="id-name">{escape(r['full_name'])}</div>
        <div class="id-tid">{escape(r['tourist_id'])}</div>
        <dl class="id-grid">
          <div><dt>Nationality</dt><dd>{escape(r['nationality'])}</dd></div>
          <div><dt>Document</dt><dd>{escape(r['doc_type'])} ••••{escape(r['doc_last4'])}</dd></div>
          <div><dt>Valid from</dt><dd>{escape(r['valid_from'][:10])}</dd></div>
          <div><dt>Valid until</dt><dd>{escape(r['valid_until'][:10])}</dd></div>
        </dl>
        <div class="id-hash">⛓️ Block hash <code class="copy" data-copy="{escape(r['block_hash'])}" title="Click to copy">{escape(r['block_hash'][:40])}…</code></div>
      </div>
    </div>
    <div class="card checks fade-up d2">
      <h3>Verification checks</h3>
      {steps}
      {f'<div class="badge bad" style="margin-top:14px">Problems found</div><ul class="text-2">{problems}</ul>' if problems else ''}
      <p class="muted" style="font-size:12.5px;margin:16px 0 0">Checked {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
        by recomputing the identity hash and every block hash. The ID document number is never stored.</p>
    </div>
  </div>
</div>"""
    return layout(f"Verify {r['tourist_id']}", body, active="verify", head=pages_css(),
                  body_class="invalid" if not valid else "")


# ─────────────────────────────────────────────
#  Ledger
# ─────────────────────────────────────────────

def ledger_html(blocks: list[dict], chain: dict) -> str:
    ok = chain["valid"]
    items = []
    for b in blocks:
        try:
            pretty = json.dumps(json.loads(b["payload"]), indent=2, ensure_ascii=False)
        except ValueError:
            pretty = b["payload"]
        items.append(f"""
<div class="card block t-{escape(b['block_type'])}" data-toggle tabindex="0" role="button" aria-expanded="false">
  <div class="block-top">
    <span class="block-idx">#{b['block_index']}</span>
    <span class="badge info">{escape(b['block_type'])}</span>
    <span class="block-time">{escape(b['timestamp'][:19].replace('T', ' '))} UTC</span>
    <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
  </div>
  <div class="block-hash"><code class="h copy" data-copy="{escape(b['hash'])}" title="Click to copy">{escape(b['hash'][:32])}…</code>
    <span class="muted">← prev <code>{escape(b['previous_hash'][:12])}…</code></span><span class="pow mono">nonce {b['nonce']}</span></div>
  <div class="block-body"><div><pre>{escape(pretty)}</pre></div></div>
</div>""")
    body = f"""
<div class="container">
  <div class="ledger-head">
    <div><span class="eyebrow fade-up">Public audit trail</span>
      <h1 class="fade-up d1">Safety Ledger</h1>
      <p class="muted fade-up d2" style="margin:10px 0 0">SHA-256 hash chain with proof-of-work. Payloads hold only IDs and hashes, no personal data.</p></div>
    <div class="card integrity {'ok' if ok else 'bad'} fade-up d2">
      <div class="shield">{'✅' if ok else '⚠️'}</div>
      <div><b>{'Chain INTACT' if ok else f"BROKEN AT #{chain['broken_at']}"}</b><br>
        <span class="muted"><span data-count="{chain['length']}">0</span> blocks verified</span></div>
    </div>
  </div>
  <div class="chain" data-stagger>{''.join(items) or '<div class="card muted">No blocks yet.</div>'}</div>
  <p class="muted reveal" style="margin-top:18px;font-size:13px">Showing the latest {len(blocks)} blocks · click a block to inspect its payload · click a hash to copy.</p>
</div>"""
    return layout("Safety Ledger", body, active="ledger", head=pages_css())


# ─────────────────────────────────────────────
#  Command center
# ─────────────────────────────────────────────

ICON_SOUND = '<svg viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></svg>'
ICON_FIT = '<svg viewBox="0 0 24 24"><path d="M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6"/></svg>'
ICON_LIST = '<svg viewBox="0 0 24 24"><path d="M12 3 2 21h20L12 3z"/></svg>'


def command_html(token: str) -> str:
    token_js = json.dumps(token).replace("</", "<\\/")
    head = ('<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
            f'<link rel="stylesheet" href="{asset("command.css")}">')
    body = f"""
<div class="app-shell">
  <header class="topbar">
    <a class="brand" href="/" title="Public portal"><span class="brand-mark">{SHIELD}</span></a>
    <div class="title"><b>Tourist Safety Command Center</b><span>Smart monitoring &amp; incident response</span></div>
    <div class="spacer"></div>
    <span class="status-pill" id="conn"><span class="dot live"></span><span id="conn-t">Connecting…</span></span>
    <span class="clock hide-md" id="clock"></span>
    <button class="icon-btn" id="fit" title="Fit all on map" aria-label="Fit all on map">{ICON_FIT}</button>
    <button class="icon-btn" id="sound" title="Alert sound off" aria-label="Toggle alert sound">{ICON_SOUND}</button>
  </header>

  <section class="kpis" id="kpis" aria-label="Key metrics"></section>

  <div class="main">
    <div class="map-wrap">
      <div id="map" role="application" aria-label="Live safety map"></div>
      <div class="overlay vignette" id="vignette"></div>
      <div class="overlay crit-banner" id="banner" role="alert"><span class="dot live"></span><span id="banner-t"></span></div>
      <div class="overlay legend">
        <div><i style="background:#4cc9f0"></i>Tourist</div>
        <div><i style="background:#ff4d6d"></i>Critical / check-in pending</div>
        <div><i style="background:#ff8c42"></i>High · <i style="background:#ffc857;margin-left:4px"></i>Medium</div>
        <div><i style="background:transparent;border:2px dashed #ff4d6d"></i>Geo-fence · 🔥 hotspot</div>
      </div>
    </div>

    <aside class="side">
      <div class="tabs" role="tablist">
        <span class="tab-ind"></span>
        <button class="tab active" data-tab="incidents" role="tab">Incidents <span class="count" id="c-incidents">0</span></button>
        <button class="tab" data-tab="tourists" role="tab">Tourists <span class="count" id="c-tourists">0</span></button>
        <button class="tab" data-tab="hotspots" role="tab">Hotspots <span class="count" id="c-hotspots">0</span></button>
        <button class="tab" data-tab="activity" role="tab">Activity</button>
      </div>
      <div class="tools">
        <input class="search" id="search" placeholder="Search name, TID, type…" aria-label="Search">
        <div class="chips" id="chips"></div>
      </div>
      <div class="list" id="list"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>
      <div class="ledger-foot" id="ledger"></div>
    </aside>
  </div>
</div>
<div class="toasts" id="toasts" aria-live="polite"></div>"""
    scripts = (f'<script>window.TB_TOKEN = {token_js};</script>'
               '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
               f'<script src="{asset("command.js")}"></script>')
    return layout("Safety Command Center", body, head=head, scripts=scripts, body_class="app", chrome=False)


# ─────────────────────────────────────────────
#  Errors
# ─────────────────────────────────────────────

def error_html(code: int, title: str, message: str, icon: str = "🛰️") -> str:
    body = f"""
<div class="container notfound">
  <div class="big">{icon}</div>
  <h1 class="fade-up">{escape(title)}</h1>
  <p class="muted fade-up d1">{escape(message)}</p>
  <a class="btn ghost fade-up d2" href="/" style="margin-top:20px">← Back to portal</a>
</div>"""
    return layout(f"{code} {title}", body, head=pages_css())


# ─────────────────────────────────────────────
#  Tourist & guardian portal
# ─────────────────────────────────────────────

def login_html(bot_username: str) -> str:
    bot = escape(bot_username)
    body = f"""
<div class="container auth-wrap">
  <div class="card auth-card glow-border fade-up">
    <div class="auth-head">
      <span class="brand-mark big">{SHIELD}</span>
      <h1>Sign in to your portal</h1>
      <p class="muted">For tourists and family members. We use your Telegram account, so there's no password.</p>
    </div>

    <form id="step1" class="auth-form" autocomplete="off">
      <label for="ident">Telegram username, Telegram ID or Tourist ID</label>
      <input class="input" id="ident" name="ident" placeholder="@yourname  ·  1440455286  ·  TID-XXXXXXXXXX" required>
      <button class="btn" type="submit" id="send">Send me a login code</button>
      <p class="hint">A 6-digit code will arrive in your chat with <a href="https://t.me/{bot}" target="_blank" rel="noopener">@{bot}</a>.
        New here? Open the bot and tap <b>START</b> first.</p>
    </form>

    <form id="step2" class="auth-form" hidden autocomplete="off">
      <label for="code">Enter the 6-digit code from Telegram</label>
      <input class="input code" id="code" inputmode="numeric" maxlength="6" pattern="[0-9]{{6}}" placeholder="••••••" required>
      <button class="btn" type="submit" id="verify">Sign in</button>
      <div class="auth-links"><button class="link-btn" type="button" id="resend">↻ Send a new code</button><button class="link-btn" type="button" id="back">← Different account</button></div>
    </form>

    <div class="auth-msg" id="msg" role="status" aria-live="polite"></div>

    <div class="auth-alt">
      <span>or</span>
      <p>Send <code>/login</code> to <a href="https://t.me/{bot}" target="_blank" rel="noopener">@{bot}</a> and tap the one-time link.</p>
    </div>
  </div>
</div>"""
    script = f'<script src="{asset("login.js")}" defer></script>'
    return layout("Sign in", body, head=f'<link rel="stylesheet" href="{asset("app.css")}">', scripts=script)


def app_html() -> str:
    head = ('<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
            f'<link rel="stylesheet" href="{asset("app.css")}">')
    body = f"""
<div class="portal">
  <header class="p-top">
    <a class="brand" href="/"><span class="brand-mark">{SHIELD}</span><span class="hide-xs">Travel Buddy</span></a>
    <div class="p-user" id="who"><span class="skel-line"></span></div>
    <a class="btn ghost sm" id="desk-link" href="/command" hidden>🖥️ Command center</a>
    <button class="icon-btn" id="logout" title="Sign out" aria-label="Sign out">
      <svg viewBox="0 0 24 24"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l-5-5 5-5M5 12h12"/></svg></button>
  </header>

  <nav class="p-tabs" id="tabs" role="tablist">
    <button data-tab="home" class="active">🛡️<span>Safety</span></button>
    <button data-tab="trip">🚆<span>Trip</span></button>
    <button data-tab="incidents">🚨<span>Incidents</span></button>
    <button data-tab="id">🪪<span>My ID</span></button>
    <button data-tab="guardians">👨‍👩‍👧<span>Guardians</span></button>
    <button data-tab="family" id="family-tab" hidden>👪<span>Family</span></button>
    <button data-tab="settings">⚙️<span>Settings</span></button>
  </nav>

  <main class="p-main" id="view"><div class="p-loading"><div class="spinner"></div>Loading your safety picture…</div></main>
</div>
<div class="toasts" id="toasts" aria-live="polite"></div>
<dialog id="dlg"></dialog>"""
    scripts = ('<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
               f'<script src="{asset("app.js")}"></script>')
    return layout("My Safety Portal", body, head=head, scripts=scripts, body_class="portal-body", chrome=False)
