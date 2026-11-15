"""
dashboard.py — Web Command Center & public verification portal (Python stdlib only).

Routes
  /                         Landing page (links to public tools)
  /command?token=…          🔒 Live command-center map for the safety desk (auto-refresh)
  /api/overview?token=…     🔒 JSON feed used by the map
  /verify/<TID>             🌐 Public Digital Tourist ID verification (target of the QR code)
  /ledger                   🌐 Public blockchain ledger explorer (hashes only, no personal data)
  /api/ledger               🌐 JSON ledger summary

Runs in a background thread next to the bot; reads SQLite directly (read-only).
"""

import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from travel_buddy.core import database
from travel_buddy.safety.ledger import compute_identity_hash, verify_blocks
from travel_buddy.safety.geofence import RISK_ORDER
from travel_buddy.safety.risk_intel import HOTSPOT_DAYS, cluster_hotspots

logger = logging.getLogger(__name__)

_token = os.getenv("DASHBOARD_TOKEN") or secrets.token_urlsafe(16)


def dashboard_token() -> str:
    return _token


def public_base_url() -> str:
    port = os.getenv("WEB_PORT", "8080")
    return os.getenv("PUBLIC_BASE_URL", f"http://localhost:{port}").rstrip("/")


# ─────────────────────────────────────────────
#  Data access (sync, read-only)
# ─────────────────────────────────────────────

def _q(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(database.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _names() -> dict[int, dict]:
    out = {r["telegram_id"]: {"name": r["username"] or f"User {r['telegram_id']}", "tid": None}
           for r in _q("SELECT telegram_id, username FROM users")}
    for r in _q("SELECT telegram_id, full_name, tourist_id FROM tourist_ids WHERE status = 'active'"):
        out[r["telegram_id"]] = {"name": r["full_name"], "tid": r["tourist_id"]}
    return out


def build_overview() -> dict:
    now = time.time()
    names = _names()
    zones = _q("SELECT * FROM geofence_zones WHERE active = 1")
    zone_by_id = {z["zone_id"]: z for z in zones}

    tourists = []
    for s in _q("SELECT * FROM safety_state WHERE last_lat IS NOT NULL AND last_seen_ts >= ?", (now - 86400,)):
        inside = json.loads(s.get("zone_state") or "{}").get("inside", [])
        risk = max((zone_by_id[z]["risk_level"] for z in inside if z in zone_by_id),
                   key=lambda r: RISK_ORDER[r], default="safe")
        who = names.get(s["telegram_id"], {"name": f"User {s['telegram_id']}", "tid": None})
        tourists.append({
            "id": s["telegram_id"], "name": who["name"], "tid": who["tid"],
            "lat": s["last_lat"], "lng": s["last_lng"],
            "minutes_ago": int((now - s["last_seen_ts"]) // 60),
            "zone_risk": risk, "checkin_pending": bool(s["checkin_pending_ts"]),
        })

    incidents = _q("SELECT * FROM incidents ORDER BY incident_id DESC LIMIT 100")
    for i in incidents:
        who = names.get(i["telegram_id"], {"name": f"User {i['telegram_id']}", "tid": None})
        i["name"], i["tid"] = who["name"], who["tid"]

    since = (datetime.now(timezone.utc) - timedelta(days=HOTSPOT_DAYS)).isoformat()
    hotspots = cluster_hotspots(_q(
        "SELECT * FROM incidents WHERE lat IS NOT NULL AND created_at >= ? "
        "AND incident_type NOT IN ('no_response', 'geofence_breach')", (since,)))

    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    open_inc = [i for i in incidents if i["status"] != "resolved"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "users": _q("SELECT COUNT(*) AS n FROM users")[0]["n"],
            "digital_ids": _q("SELECT COUNT(*) AS n FROM tourist_ids WHERE status='active'")[0]["n"],
            "active_trips": _q("SELECT COUNT(*) AS n FROM trips WHERE status='active'")[0]["n"],
            "live_tourists": sum(1 for t in tourists if t["minutes_ago"] <= 60),
            "open_incidents": len(open_inc),
            "critical": sum(1 for i in open_inc if i["severity"] == "critical"),
            "pending_checkins": sum(1 for t in tourists if t["checkin_pending"]),
            "in_risk_zones": sum(1 for t in tourists if RISK_ORDER[t["zone_risk"]] >= RISK_ORDER["high"]),
            "hotspots": len([h for h in hotspots if h["risk_level"] != "safe"]),
        },
        "ledger": {"valid": chain["valid"], "length": chain["length"], "broken_at": chain["broken_at"],
                   "head": chain.get("head")},
        "tourists": tourists,
        "incidents": incidents,
        "zones": zones,
        "hotspots": hotspots,
    }


def verify_tid_sync(tid: str) -> dict:
    tid = tid.upper()
    res = {"tourist_id": tid, "found": False, "valid": False, "reasons": []}
    rows = _q("SELECT * FROM tourist_ids WHERE tourist_id = ?", (tid,))
    if not rows:
        res["reasons"].append("No such Tourist ID on record.")
        return res
    rec = rows[0]
    res.update(found=True, record=rec)
    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    res["chain"] = chain
    if not chain["valid"]:
        res["reasons"].append(f"Ledger integrity broken at block #{chain['broken_at']}.")
    block = _q("SELECT * FROM ledger WHERE hash = ?", (rec["block_hash"],))
    if not block:
        res["reasons"].append("Sealing block missing from ledger.")
    else:
        res["block_index"] = block[0]["block_index"]
        if json.loads(block[0]["payload"]).get("identity_hash") != compute_identity_hash(rec):
            res["reasons"].append("Record altered since issue (hash mismatch).")
    if rec["status"] != "active":
        res["reasons"].append("ID has been revoked.")
    if datetime.fromisoformat(rec["valid_until"]) < datetime.now(timezone.utc):
        res["reasons"].append("ID validity period has expired.")
    res["valid"] = not res["reasons"]
    return res


# ─────────────────────────────────────────────
#  HTML
# ─────────────────────────────────────────────

BASE_CSS = """
:root{--bg:#070b14;--panel:rgba(18,26,44,.82);--line:rgba(120,160,255,.18);--text:#e6ecff;--muted:#8b98b8;
--accent:#4cc9f0;--ok:#2ee59d;--warn:#ffc857;--high:#ff8c42;--crit:#ff4d6d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#132447 0,#070b14 60%);
color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--accent)}code{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;backdrop-filter:blur(8px)}
h1,h2,h3{margin:0 0 8px}.muted{color:var(--muted)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}
.ok{background:rgba(46,229,157,.15);color:var(--ok)}.bad{background:rgba(255,77,109,.15);color:var(--crit)}
"""


def page(title: str, body: str, extra_head: str = "") -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{escape(title)}</title>"
            f"<style>{BASE_CSS}</style>{extra_head}</head><body>{body}</body></html>")


def landing_html() -> str:
    return page("Travel Buddy Safety Portal", """
<div style="max-width:720px;margin:60px auto;padding:0 16px">
 <h1>🛡️ Travel Buddy — Tourist Safety Portal</h1>
 <p class="muted">Blockchain-verified tourist identity, AI incident response and live geo-fenced monitoring.</p>
 <div class="card" style="margin:18px 0">
  <h3>🪪 Verify a Digital Tourist ID</h3>
  <form onsubmit="location.href='/verify/'+encodeURIComponent(this.t.value.trim());return false">
   <input name="t" placeholder="TID-XXXXXXXXXX" style="padding:10px;border-radius:8px;border:1px solid var(--line);
    background:#0d1526;color:var(--text);width:70%"> <button style="padding:10px 16px;border-radius:8px;border:0;
    background:var(--accent);color:#04121c;font-weight:700">Verify</button></form>
 </div>
 <div class="card"><h3>⛓️ <a href="/ledger">Public safety ledger explorer →</a></h3>
 <p class="muted">Every ID, SOS and incident is sealed in a hash-chained, proof-of-work ledger.</p></div>
 <p class="muted" style="margin-top:24px">Safety desk? Open the command center link sent to you by the bot (/web).</p>
</div>""")


def verify_html(res: dict) -> str:
    if not res["found"]:
        body = f"<h1>❌ Not found</h1><p><code>{escape(res['tourist_id'])}</code> is not on record.</p>"
    else:
        r = res["record"]
        status = ("<span class='badge ok'>VALID</span>" if res["valid"] else "<span class='badge bad'>INVALID</span>")
        problems = "".join(f"<li>{escape(x)}</li>" for x in res["reasons"])
        body = f"""
<h1>🪪 Digital Tourist ID {status}</h1>
<div class="card" style="margin-top:14px">
 <table style="width:100%;border-collapse:collapse">
 <tr><td class="muted">Tourist ID</td><td><code>{escape(r['tourist_id'])}</code></td></tr>
 <tr><td class="muted">Name</td><td><b>{escape(r['full_name'])}</b></td></tr>
 <tr><td class="muted">Nationality</td><td>{escape(r['nationality'])}</td></tr>
 <tr><td class="muted">Document</td><td>{escape(r['doc_type'])} ••••{escape(r['doc_last4'])}</td></tr>
 <tr><td class="muted">Valid</td><td>{escape(r['valid_from'][:10])} → {escape(r['valid_until'][:10])}</td></tr>
 <tr><td class="muted">Sealed in block</td><td>#{res.get('block_index', '?')} · <code>{escape(r['block_hash'])}</code></td></tr>
 <tr><td class="muted">Ledger</td><td>{res['chain']['length']} blocks — {'intact ✅' if res['chain']['valid'] else 'BROKEN ❌'}</td></tr>
 </table>
 {f'<h3 style="margin-top:12px">Problems</h3><ul>{problems}</ul>' if problems else ''}
</div>
<p class="muted">Verified {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by recomputing the identity
hash and every block hash. The ID document number is never stored.</p>"""
    return page("Verify Tourist ID", f"<div style='max-width:720px;margin:40px auto;padding:0 16px'>{body}"
                f"<p><a href='/'>← Portal</a></p></div>")


def ledger_html() -> str:
    blocks = _q("SELECT * FROM ledger ORDER BY block_index DESC LIMIT 60")
    chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
    rows = "".join(
        f"<tr><td>#{b['block_index']}</td><td><span class='badge ok'>{escape(b['block_type'])}</span></td>"
        f"<td class='muted'>{escape(b['timestamp'][:19].replace('T', ' '))}</td>"
        f"<td><code>{escape(b['hash'][:28])}…</code><br><span class='muted'>prev <code>{escape(b['previous_hash'][:16])}…</code>"
        f" · nonce {b['nonce']}</span></td><td><code>{escape(b['payload'][:140])}</code></td></tr>"
        for b in blocks)
    status = ("<span class='badge ok'>INTACT</span>" if chain["valid"]
              else f"<span class='badge bad'>BROKEN AT #{chain['broken_at']}</span>")
    return page("Safety Ledger", f"""
<div style="max-width:1100px;margin:30px auto;padding:0 16px">
<h1>⛓️ Safety Ledger {status}</h1>
<p class="muted">{chain['length']} blocks · SHA-256 hash chain with proof-of-work · payloads contain only IDs and hashes.</p>
<div class="card" style="overflow-x:auto"><table style="width:100%;border-collapse:collapse" cellpadding="8">
<tr class="muted"><th align=left>Block</th><th align=left>Type</th><th align=left>Time (UTC)</th><th align=left>Hash</th>
<th align=left>Payload</th></tr>{rows}</table></div><p><a href="/">← Portal</a></p></div>""")


def command_html(token: str) -> str:
    head = ('<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
            '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>')
    return page("Safety Command Center", COMMAND_BODY.replace("__TOKEN__", json.dumps(token)), head)


COMMAND_BODY = r"""
<style>
#app{display:grid;grid-template-columns:1fr 380px;grid-template-rows:auto 1fr;height:100vh;gap:12px;padding:12px}
header{grid-column:1/3;display:flex;align-items:center;gap:12px 16px;flex-wrap:wrap}
#map{border-radius:14px;border:1px solid var(--line);min-height:300px;background:#0b1220}
.dark-tiles{filter:invert(1) hue-rotate(180deg) brightness(.8) contrast(.9) saturate(.6)}
.leaflet-popup-content{color:#111}
aside{overflow:auto;display:flex;flex-direction:column;gap:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;flex:1 1 100%}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 10px}
.tile b{display:block;font-size:22px}.tile span{color:var(--muted);font-size:12px}
.item{padding:8px;border-radius:10px;border:1px solid var(--line);margin-bottom:6px;cursor:pointer}
.item:hover{border-color:var(--accent)}
.sev-critical{border-left:4px solid var(--crit)}.sev-high{border-left:4px solid var(--high)}
.sev-medium{border-left:4px solid var(--warn)}.sev-low{border-left:4px solid var(--accent)}
.live{width:10px;height:10px;border-radius:50%;background:var(--ok);display:inline-block;box-shadow:0 0 12px var(--ok);
animation:p 1.6s infinite}@keyframes p{50%{opacity:.3}}
.pulse{animation:p 1.2s infinite}
@media(max-width:900px){#app{grid-template-columns:1fr;grid-template-rows:auto 55vh auto;height:auto}header{grid-column:1}}
</style>
<div id="app">
 <header>
  <h2 style="margin:0">🛡️ Tourist Safety Command Center</h2><span class="live"></span>
  <span class="muted" id="upd">connecting…</span>
  <div class="tiles" id="tiles"></div>
 </header>
 <div id="map"></div>
 <aside>
  <div class="card"><h3>🚨 Incident queue</h3><div id="inc"></div></div>
  <div class="card"><h3>🧭 Tourists (24 h)</h3><div id="tour"></div></div>
  <div class="card"><h3>🔥 Learned hotspots</h3><div id="hot"></div></div>
  <div class="card"><h3>⛓️ Ledger</h3><div id="led"></div></div>
 </aside>
</div>
<script>
const TOKEN = __TOKEN__;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const RISK = {safe:'#2ee59d',caution:'#ffc857',high:'#ff8c42',restricted:'#ff4d6d'};
const SEV = {critical:'#ff4d6d',high:'#ff8c42',medium:'#ffc857',low:'#4cc9f0'};
const map = L.map('map',{zoomControl:true}).setView([22.5,82],5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
 {attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom:19,className:'dark-tiles'}).addTo(map);
new ResizeObserver(() => map.invalidateSize()).observe(document.getElementById('map'));
const layers = {zones:L.layerGroup().addTo(map),hot:L.layerGroup().addTo(map),
                inc:L.layerGroup().addTo(map),tour:L.layerGroup().addTo(map)};
L.control.layers(null,{'Geo-fence zones':layers.zones,'Learned hotspots':layers.hot,
 'Incidents':layers.inc,'Tourists':layers.tour}).addTo(map);
let first = true;
function tile(v,l,c){return `<div class="tile"><b style="color:${c||'inherit'}">${v}</b><span>${l}</span></div>`}
async function refresh(){
 let d; try{ d = await (await fetch('/api/overview?token='+encodeURIComponent(TOKEN))).json(); }
 catch(e){ document.getElementById('upd').textContent='offline — retrying'; return; }
 const s = d.stats;
 document.getElementById('tiles').innerHTML = tile(s.live_tourists,'live tourists')+tile(s.active_trips,'active trips')
  +tile(s.open_incidents,'open incidents',s.open_incidents?'#ff8c42':'')+tile(s.critical,'critical',s.critical?'#ff4d6d':'')
  +tile(s.pending_checkins,'pending check-ins')+tile(s.in_risk_zones,'in risk zones')+tile(s.hotspots,'hotspots')
  +tile(s.digital_ids,'digital IDs');
 document.getElementById('upd').textContent='updated '+new Date().toLocaleTimeString();
 Object.values(layers).forEach(l=>l.clearLayers());
 const bounds=[];
 d.zones.forEach(z=>{L.circle([z.center_lat,z.center_lng],{radius:z.radius_m,color:RISK[z.risk_level],weight:1,
  fillOpacity:.12,dashArray:'4 4'}).bindPopup(`<b>${esc(z.name)}</b><br>${z.risk_level}<br>${esc(z.advisory)}`).addTo(layers.zones)});
 d.hotspots.filter(h=>h.risk_level!=='safe').forEach(h=>{L.circle([h.lat,h.lng],{radius:h.radius_m,color:RISK[h.risk_level],
  weight:2,fillOpacity:.25,className:'pulse'}).bindPopup(`<b>🔥 Hotspot ${h.hotspot_id}</b><br>${h.count} incidents · score ${h.score}
  <br>mostly ${esc(h.dominant_type)}`).addTo(layers.hot); bounds.push([h.lat,h.lng]);});
 const open = d.incidents.filter(i=>i.status!=='resolved');
 open.forEach(i=>{ if(i.lat==null) return; bounds.push([i.lat,i.lng]);
  L.circleMarker([i.lat,i.lng],{radius:i.severity==='critical'?11:8,color:'#fff',weight:1,fillColor:SEV[i.severity],
  fillOpacity:.95,className:i.severity==='critical'?'pulse':''}).bindPopup(`<b>#${i.incident_id} ${esc(i.incident_type)}</b>
  (${i.severity})<br>${esc(i.name)} ${i.tid?'· '+esc(i.tid):''}<br>${esc(i.description)}<br><i>${esc(i.status)}</i>`).addTo(layers.inc)});
 d.tourists.forEach(t=>{ bounds.push([t.lat,t.lng]);
  L.circleMarker([t.lat,t.lng],{radius:6,color:t.checkin_pending?'#ff4d6d':'#4cc9f0',weight:2,fillColor:'#0b1220',fillOpacity:1})
  .bindPopup(`<b>${esc(t.name)}</b> ${t.tid?'· '+esc(t.tid):''}<br>seen ${t.minutes_ago} min ago<br>zone: ${t.zone_risk}
  ${t.checkin_pending?'<br><b style="color:#ff4d6d">check-in pending</b>':''}`).addTo(layers.tour)});
 if(first && bounds.length){ map.invalidateSize(); map.fitBounds(bounds,{padding:[40,40],maxZoom:13}); first=false; }
 document.getElementById('inc').innerHTML = open.length ? open.map(i=>`<div class="item sev-${i.severity}"
  onclick="${i.lat!=null?`map.flyTo([${i.lat},${i.lng}],15)`:''}"><b>#${i.incident_id}</b> ${esc(i.incident_type)} ·
  <b style="color:${SEV[i.severity]}">${i.severity}</b> · ${esc(i.status)}<br><span class="muted">${esc(i.name)} ·
  ${esc((i.created_at||'').slice(0,16).replace('T',' '))}</span><br>${esc((i.description||'').slice(0,120))}</div>`).join('')
  : '<span class="muted">No open incidents 🙌</span>';
 document.getElementById('tour').innerHTML = d.tourists.length ? d.tourists.map(t=>`<div class="item"
  onclick="map.flyTo([${t.lat},${t.lng}],15)"><b>${esc(t.name)}</b> <span class="muted">${t.tid?esc(t.tid):''}</span><br>
  <span style="color:${RISK[t.zone_risk]}">● ${t.zone_risk}</span> · ${t.minutes_ago} min ago
  ${t.checkin_pending?' · <b style="color:#ff4d6d">check-in pending</b>':''}</div>`).join('') : '<span class="muted">No tourists seen</span>';
 const hs = d.hotspots.filter(h=>h.risk_level!=='safe');
 document.getElementById('hot').innerHTML = hs.length ? hs.map(h=>`<div class="item" onclick="map.flyTo([${h.lat},${h.lng}],15)">
  🔥 <b>${h.hotspot_id}</b> · ${h.count} incidents · <span style="color:${RISK[h.risk_level]}">${h.risk_level}</span>
  <br><span class="muted">mostly ${esc(h.dominant_type)} · score ${h.score}</span></div>`).join('')
  : '<span class="muted">Not enough incident data yet — hotspots appear when incidents cluster.</span>';
 const l = d.ledger;
 document.getElementById('led').innerHTML = `${l.length} blocks — ${l.valid?'<span class="badge ok">INTACT</span>'
  :'<span class="badge bad">BROKEN at #'+l.broken_at+'</span>'}<br><code>${esc(l.head||'')}</code><br><a href="/ledger" target="_blank">explore →</a>`;
}
refresh(); setInterval(refresh, 10000);
</script>
"""


# ─────────────────────────────────────────────
#  Server
# ─────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "TravelBuddySafety/1.0"

    def log_message(self, fmt, *args):  # route to our logger, quietly
        logger.debug("web %s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self, qs: dict) -> bool:
        given = (qs.get("token") or [""])[0]
        return bool(given) and hmac.compare_digest(given, _token)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/":
                return self._send(200, landing_html())
            if path.startswith("/verify/"):
                return self._send(200, verify_html(verify_tid_sync(path.split("/verify/", 1)[1][:32])))
            if path == "/ledger":
                return self._send(200, ledger_html())
            if path == "/api/ledger":
                chain = verify_blocks(_q("SELECT * FROM ledger ORDER BY block_index ASC"))
                return self._send(200, json.dumps(chain), "application/json")
            if path in ("/command", "/api/overview"):
                if not self._authorised(qs):
                    return self._send(403, page("Forbidden", "<h1 style='margin:40px'>🔒 Invalid or missing token</h1>"))
                if path == "/command":
                    return self._send(200, command_html((qs.get("token") or [""])[0]))
                return self._send(200, json.dumps(build_overview(), default=str), "application/json")
            return self._send(404, page("Not found", "<h1 style='margin:40px'>404</h1>"))
        except Exception:
            logger.exception("Web request failed: %s", self.path)
            return self._send(500, page("Error", "<h1 style='margin:40px'>Server error</h1>"))


_server: ThreadingHTTPServer | None = None


def start_web_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer | None:
    global _server
    host = host or os.getenv("WEB_HOST", "127.0.0.1")
    port = port if port is not None else int(os.getenv("WEB_PORT", "8080"))
    try:
        _server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        logger.warning("Web command center not started (%s:%s): %s", host, port, exc)
        return None
    threading.Thread(target=_server.serve_forever, name="web-dashboard", daemon=True).start()
    logger.info("Web command center: %s/command?token=%s", public_base_url(), _token)
    return _server


def stop_web_server() -> None:
    if _server:
        _server.shutdown()
