/* Travel Buddy — tourist & guardian portal (same features as the Telegram bot) */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const RISK = { safe: "#2ee59d", caution: "#ffc857", high: "#ff8c42", restricted: "#ff4d6d" };
  const SCORE_COLOR = { Safe: "#2ee59d", Moderate: "#ffc857", "Elevated risk": "#ff8c42", "High risk": "#ff4d6d" };
  const TYPES = [["medical", "🩺 Medical"], ["theft", "👜 Theft / loss"], ["harassment", "⚠️ Harassment"], ["accident", "🚗 Accident"],
    ["lost", "🧭 Lost / missing"], ["scam", "💸 Scam / fraud"], ["natural_hazard", "🌪️ Natural hazard"], ["other", "📝 Other"]];

  const S = { me: null, ov: null, family: null, tab: "home", notes: [], maps: {}, watch: null, lastSent: 0, loading: false,
              chat: [], phrases: null, prefs: null, prefsLoaded: false, editPrefs: false, lang: null };

  // ───────────── helpers ─────────────
  async function api(method, url, data) {
    const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "TravelBuddy" } };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRF"] = S.me?.csrf || "";
      opts.body = JSON.stringify(data || {});
    }
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = "/login"; throw new Error("Please sign in."); }
    const body = await r.json().catch(() => ({ ok: false, error: "Unexpected server response." }));
    if (!r.ok || !body.ok) throw new Error(body.error || "Something went wrong.");
    return body;
  }
  const ago = ts => {
    if (!ts) return "never";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  };
  const isoAgo = iso => iso ? ago(Date.parse(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z") / 1000) : "";
  function toast(html, kind = "info") {
    const el = document.createElement("div");
    el.className = "toast";
    el.style.setProperty("--tc", { ok: "#2ee59d", bad: "#ff4d6d", warn: "#ffc857" }[kind] || "#4cc9f0");
    el.innerHTML = html;
    $("#toasts").prepend(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 350); }, kind === "bad" ? 7000 : 5000);
  }
  function dialog(html, buttons = [["Close", "ghost"]]) {
    const d = $("#dlg");
    d.innerHTML = `<div class="dlg-body">${html}</div><div class="dlg-actions">${buttons.map(([t, c], i) =>
      `<button class="btn ${c || ""}" data-i="${i}">${t}</button>`).join("")}</div>`;
    return new Promise(res => {
      d.onclick = e => { const b = e.target.closest("button[data-i]"); if (b) { d.close(); res(+b.dataset.i); } };
      d.oncancel = () => res(-1);
      d.showModal();
    });
  }
  async function run(btn, fn) {
    const label = btn?.innerHTML;
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px"></span>'; }
    try { return await fn(); }
    catch (e) { toast(esc(e.message), "bad"); }
    finally { if (btn) { btn.disabled = false; btn.innerHTML = label; } }
  }
  const getPosition = (timeout = 8000) => new Promise((res, rej) => {
    if (!navigator.geolocation) return rej(new Error("Location isn't available in this browser."));
    navigator.geolocation.getCurrentPosition(p => res({ lat: p.coords.latitude, lng: p.coords.longitude }),
      err => rej(new Error(err.code === 1 ? "Location permission was denied. Allow it in your browser settings." : "Couldn't get your location.")),
      { enableHighAccuracy: true, timeout, maximumAge: 15000 });
  });

  // ───────────── maps ─────────────
  function makeMap(id) {
    S.maps[id]?.remove();
    const m = L.map(id, { zoomControl: true, attributionControl: true }).setView([22.5, 82], 4);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>', maxZoom: 19, className: "dark-tiles",
    }).addTo(m);
    S.maps[id] = m;
    setTimeout(() => m.invalidateSize(), 60);
    return m;
  }
  const pin = (cls) => L.divIcon({ className: "", iconSize: [18, 18], iconAnchor: [9, 9], html: `<div class="mk ${cls}"><div class="core"></div><div class="ring2"></div></div>` });
  function drawSafetyMap(m, ov) {
    const pts = [];
    (ov.zones_near || []).forEach(z => L.circle([z.lat, z.lng], { radius: z.radius_m, color: RISK[z.risk], weight: 1.5, fillOpacity: .08, dashArray: "6 6" })
      .bindPopup(`<b>${esc(z.name)}</b><br>${z.risk} zone<br>${esc(z.advisory || "")}`).addTo(m));
    (ov.hotspots || []).forEach(h => L.circle([h.lat, h.lng], { radius: h.radius_m, color: RISK[h.risk_level], weight: 2, fillOpacity: .2 })
      .bindPopup(`<b>🔥 Incident hotspot</b><br>${h.count} recent ${esc(h.dominant_type)} reports`).addTo(m));
    if (ov.trail?.length > 1) L.polyline(ov.trail.map(p => [p.lat, p.lng]), { color: "#4cc9f0", weight: 3, opacity: .6, dashArray: "4 6" }).addTo(m);
    if (ov.trip) { L.marker([ov.trip.destination_lat, ov.trip.destination_lng], { icon: pin("dest") }).bindPopup(`🏁 ${esc(ov.trip.destination_name)}`).addTo(m); pts.push([ov.trip.destination_lat, ov.trip.destination_lng]); }
    if (ov.location) { L.marker([ov.location.lat, ov.location.lng], { icon: pin(ov.checkin_pending ? "alert" : "me") }).bindPopup(`📍 You · ${ago(ov.location.seen_ts)}`).addTo(m); pts.push([ov.location.lat, ov.location.lng]); }
    if (pts.length === 1) m.setView(pts[0], 14);
    else if (pts.length) m.fitBounds(pts, { padding: [40, 40], maxZoom: 15 });
  }

  // ───────────── header & tabs ─────────────
  function renderHeader() {
    const me = S.me;
    const initials = (me.name || "?").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
    const role = [me.tourist_id ? esc(me.tourist_id) : "Tourist", me.guarding ? `Guardian of ${me.guarding}` : "", me.is_admin ? "Safety desk" : ""].filter(Boolean).join(" · ");
    $("#who").innerHTML = `<div class="avatar">${esc(initials)}</div><div style="min-width:0"><div class="nm">${esc(me.name)}</div><div class="role">${role}</div></div>`;
    $("#family-tab").hidden = !me.guarding;
    $("#desk-link").hidden = !me.is_admin;
  }
  function setTab(tab, push = true) {
    if (tab === "family" && !S.me?.guarding) tab = "home";
    S.tab = tab;
    $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    if (push) { history.replaceState(null, "", "#" + tab); window.scrollTo({ top: 0 }); }
    render();
    if (tab === "assistant" && (!S.phrases || !S.prefsLoaded)) loadAssistant();
  }
  $("#tabs").addEventListener("click", e => { const b = e.target.closest("button[data-tab]"); if (b) setTab(b.dataset.tab); });
  window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t && t !== S.tab && S.ov) setTab(t, false); });
  $("#logout").addEventListener("click", async () => { await api("POST", "/api/auth/logout").catch(() => {}); location.href = "/login"; });

  // ───────────── views ─────────────
  const V = {};

  V.home = () => {
    const ov = S.ov, sc = ov.score;
    const color = SCORE_COLOR[sc.label] || "#4cc9f0";
    const openInc = ov.incidents.filter(i => i.status !== "resolved");
    const zone = ov.zones_inside.length ? ov.zones_inside.map(z => `<span style="color:${RISK[z.risk]}">●</span> ${esc(z.name)}`).join(", ") : "🟢 No risk zone";
    const w = ov.weather;
    return `
    ${ov.checkin_pending ? `<div class="alert-bar">🛡️<b>Safety check-in waiting — are you okay? If you don't answer, your guardians and the safety desk will be alerted.</b><button class="btn ok sm" data-act="safe">✅ I'm safe</button></div>` : ""}
    <div class="page-title"><div><h1>Hi ${esc((S.me.name || "").split(" ")[0])} 👋</h1><p>${ov.place ? "📍 " + esc(ov.place) : "Share your location to see your live safety picture."}</p></div>
      <button class="btn ghost sm" data-act="locate">📍 Share my location</button></div>
    <div class="grid g-hero">
      <div class="card score-card">
        <div class="ring" style="--rc:${color}"><svg viewBox="0 0 150 150"><circle class="bg" cx="75" cy="75" r="68"/><circle class="fg" cx="75" cy="75" r="68" data-score="${sc.score}"/></svg>
          <div class="val"><div><b data-count="${sc.score}">0</b><span>/ 100</span></div></div></div>
        <div class="score-info"><span class="eyebrow">Safety score</span><h2 style="color:${color}">${esc(sc.label)}</h2>
          ${sc.factors.length ? `<ul class="factors">${sc.factors.map(f => `<li>${esc(f)}</li>`).join("")}</ul>` : '<p class="muted" style="margin:8px 0 0">No risk factors right now. Enjoy your trip!</p>'}</div>
      </div>
      <div class="card sos-card">
        <button class="sos-btn" id="sos" aria-label="Hold for 1.5 seconds to send SOS">SOS<small>hold 1.5 s</small></button>
        <p class="muted" style="margin:0;font-size:13px">Alerts your guardians and the safety desk with your location.</p>
        <div class="sos-actions"><button class="btn ok sm" data-act="safe">✅ I'm safe</button><button class="btn ghost sm" data-act="nearby">🏥 Nearby help</button></div>
      </div>
    </div>
    <div class="tiles" style="margin-top:16px">
      <div class="tile"><span class="k">Last location</span><span class="v">${ov.location ? ago(ov.location.seen_ts) : "not shared"}</span></div>
      <div class="tile ${ov.zones_inside.some(z => z.risk === "restricted" || z.risk === "high") ? "bad" : ov.zones_inside.length ? "warn" : ""}"><span class="k">Zone</span><span class="v">${zone}</span></div>
      <div class="tile ${w && w.worst === "high" ? "bad" : w && w.worst === "medium" ? "warn" : ""}"><span class="k">Weather</span><span class="v">${w ? esc(w.summary) + (w.hazards.length ? " ⚠️" : "") : "—"}</span></div>
      <div class="tile"><span class="k">Trip</span><span class="v">${ov.trip ? `🚆 ${esc(ov.trip.destination_name)}${ov.trip.distance_km != null ? ` · ${ov.trip.distance_km} km` : ""}` : "none"}</span></div>
      <div class="tile ${ov.timer_due_ts ? "warn" : ""}"><span class="k">Safety timer</span><span class="v">${ov.timer_due_ts ? `due in ${Math.max(0, Math.round((ov.timer_due_ts - Date.now() / 1000) / 60))} min` : "off"}</span></div>
      <div class="tile ${openInc.length ? "bad" : ""}"><span class="k">Open incidents</span><span class="v">${openInc.length}</span></div>
      <div class="tile"><span class="k">Digital ID</span><span class="v">${ov.digital_id ? "🪪 " + esc(ov.digital_id.tourist_id) : "not registered"}</span></div>
      <div class="tile ${ov.guardians.some(g => g.linked) ? "" : "warn"}"><span class="k">Guardians</span><span class="v">${ov.guardians.filter(g => g.linked).length} linked</span></div>
    </div>
    ${w && w.hazards.length ? `<div class="card" style="margin-top:16px"><h3>🌩️ Weather hazards near you</h3><div class="notes">${w.hazards.map(h => `<div class="note">${esc(h.text)}</div>`).join("")}</div></div>` : ""}
    <div class="grid g-2" style="margin-top:16px">
      <div class="card"><h3>🗺️ Your safety map</h3><p class="sub">Your position, recent trail, nearby risk zones and learned hotspots.</p><div class="map" id="map-home"></div></div>
      <div class="card"><h3>📡 Live safety notes</h3><p class="sub">Alerts from your latest location updates appear here (and in Telegram).</p>
        <div class="notes" id="notes">${S.notes.length ? S.notes.map(n => `<div class="note">${n}</div>`).join("") : '<div class="empty"><span class="e">🛰️</span>Share your location to get geo-fence and hotspot alerts.</div>'}</div></div>
    </div>`;
  };

  V.trip = () => {
    const t = S.ov.trip;
    const live = S.watch !== null;
    return `
    <div class="page-title"><div><h1>🚆 Journey</h1><p>Get woken up before your stop, and check how safe a route is.</p></div></div>
    <div class="grid g-2">
      <div class="card">${t ? `
        <h2>To ${esc(t.destination_name)}</h2><p class="sub">${t.transport_mode === "bus" ? "🚌 Bus" : "🚆 Train"} · status <b>${esc(t.status)}</b></p>
        <div class="tiles"><div class="tile"><span class="k">Distance</span><span class="v">${t.distance_km != null ? t.distance_km + " km" : "share location"}</span></div>
          <div class="tile"><span class="k">ETA</span><span class="v">${t.eta_min != null ? "~" + t.eta_min + " min" : "—"}</span></div>
          <div class="tile ${t.alert_sent ? "warn" : ""}"><span class="k">Stop alert</span><span class="v">${t.alert_sent ? "sent" : "armed"}</span></div></div>
        <div class="switch" style="margin-top:12px"><div><b>Live tracking from this device</b><span>Keep this page open; your location is sent every ~20 s.</span></div>
          <label class="toggle"><input type="checkbox" id="live" ${live ? "checked" : ""}><i></i></label></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px"><button class="btn ok sm" data-act="trip-end">🏁 I've arrived</button><button class="btn ghost sm" data-act="trip-cancel">Cancel trip</button></div>`
      : `
        <h2>Start a journey</h2><p class="sub">We'll alert you about 4 minutes (or 1 km) before your stop.</p>
        <form class="form" id="f-trip"><div class="field"><label>Destination station or address</label><input class="input" name="destination" placeholder="e.g. Jaipur Junction" required maxlength="120"></div>
          <div class="field"><label>Transport</label><div class="seg" id="mode"><button type="button" class="on" data-v="train">🚆 Train</button><button type="button" data-v="bus">🚌 Bus</button></div></div>
          <button class="btn" type="submit">Start trip</button></form>`}
      </div>
      <div class="card"><h2>🧭 Safe-route check</h2><p class="sub">Scores the road route against risk zones, hotspots, weather and night-time.</p>
        <form class="form" id="f-route"><div class="field"><label>Where are you going?</label><input class="input" name="destination" placeholder="e.g. Hawa Mahal, Jaipur" required maxlength="120"></div>
          <button class="btn" type="submit">Check route</button></form><div id="route-out"></div></div>
    </div>
    ${t ? '<div class="card" style="margin-top:16px"><h3>🗺️ Trip map</h3><div class="map" id="map-trip"></div></div>' : ""}`;
  };

  V.incidents = () => {
    const list = S.ov.incidents;
    return `
    <div class="page-title"><div><h1>🚨 Incidents</h1><p>Report a problem, follow its status and download a police report draft.</p></div></div>
    <div class="grid g-2">
      <div class="card"><h2>Report an incident</h2><p class="sub">AI triage sets the severity and alerts your guardians / the safety desk when needed. For emergencies use SOS.</p>
        <form class="form" id="f-report">
          <div class="field"><label>What happened?</label><select name="type">${TYPES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></div>
          <div class="field"><label>Describe it</label><textarea name="description" required maxlength="1000" placeholder="Where, when, anyone hurt?"></textarea></div>
          <label class="check"><input type="checkbox" name="use_location" checked> Attach my last known location</label>
          <button class="btn" type="submit">Submit report</button></form>
        <h2 style="margin-top:26px">📸 Photo evidence</h2><p class="sub">The photo's fingerprint is sealed on the ledger (proof it was never edited) and it's sent to your responders.</p>
        <form class="form" id="f-evidence">
          <div class="field"><label>Attach to</label><select name="incident_id"><option value="">Latest open incident (or a new one from the caption)</option>
            ${list.filter(i => i.status !== "resolved").map(i => `<option value="${i.id}">#${i.id} ${esc(i.label)}</option>`).join("")}</select></div>
          <div class="field"><label>Photo</label><input class="input" type="file" name="photo" accept="image/*" capture="environment" required></div>
          <div class="field"><label>Caption</label><input class="input" name="caption" maxlength="500" placeholder="What does it show?"></div>
          <button class="btn ghost" type="submit">Seal &amp; upload</button></form></div>
      <div class="card"><h2>Your incidents</h2><p class="sub">${list.length} on record</p>
        <div class="list">${list.length ? list.map(i => `
          <div class="row sev-${i.severity}"><div class="grow"><div class="t">#${i.id} ${esc(i.label)} <span class="pill ${i.severity}">${i.severity}</span> <span class="pill ${i.status}">${i.status}</span></div>
            <div class="s">${isoAgo(i.created_at)}${i.lat != null ? ` · <a href="https://www.google.com/maps?q=${i.lat},${i.lng}" target="_blank" rel="noopener">map</a>` : ""}</div>
            ${i.description ? `<div class="d">${esc(i.description)}</div>` : ""}${i.advice ? `<div class="d">💡 ${esc(i.advice)}</div>` : ""}</div>
            <div class="acts"><a class="btn ghost xs" href="/api/incident/${i.id}/efir">📄 E-FIR</a>${i.status !== "resolved" ? `<button class="btn ok xs" data-act="resolve" data-id="${i.id}">Resolve</button>` : ""}</div></div>`).join("")
          : '<div class="empty"><span class="e">🙌</span>No incidents. Stay safe!</div>'}</div></div>
    </div>`;
  };

  V.assistant = () => {
    const ph = S.phrases, lang = S.lang || ph?.auto || "hindi";
    const prefs = S.prefs;
    return `
    <div class="page-title"><div><h1>🤖 Assistant</h1><p>AI safety copilot, destination safety briefing, emergency phrases and a personal travel guide.</p></div></div>
    <div class="grid g-2">
      <div class="card span-2"><h2>💬 Safety copilot</h2><p class="sub">Ask anything. It knows your live location, zone, weather, nearby hotspots and trip.</p>
        <div class="chat" id="chat">${S.chat.length ? S.chat.map(m => `<div class="msg ${m.who}">${m.html}</div>`).join("")
          : '<div class="msg bot">👋 Hi! Try “Is it safe to walk back to my hotel now?” or “What should I avoid around here at night?”</div>'}</div>
        <form class="chat-form" id="f-ask"><input class="input" name="question" placeholder="Type your question…" autocomplete="off" maxlength="800" required>
          <button class="btn" type="submit">Ask</button></form></div>

      <div class="card"><h2>🛡️ Safety briefing</h2><p class="sub">Scams, risky areas, local hazards and emergency numbers for any destination.</p>
        <form class="form" id="f-tips"><div class="field"><label>Place</label><input class="input" name="place" maxlength="80" placeholder="Leave empty for your trip / current area"></div>
          <button class="btn" type="submit">Get briefing</button></form><div id="tips-out"></div></div>

      <div class="card"><h2>🗣️ Emergency phrases</h2><p class="sub">Show these to a local person. Auto-picked for where you are.</p>
        ${ph ? `<div class="field"><select id="lang">${Object.keys(ph.phrases).map(l => `<option value="${l}" ${l === lang ? "selected" : ""}>${l[0].toUpperCase() + l.slice(1)}</option>`).join("")}</select></div>
          <div class="phrases">${ph.keys.map((k, i) => `<div class="phrase"><span>${esc(k)}</span><b>${esc(ph.phrases[lang][i])}</b></div>`).join("")}</div>`
          : '<div class="p-loading"><div class="spinner"></div></div>'}
        <form class="form" id="f-translate" style="margin-top:14px"><div class="two"><div class="field"><label>Any language</label><input class="input" name="language" placeholder="e.g. Assamese" maxlength="30" required></div>
          <div class="field"><label>Phrase</label><input class="input" name="text" placeholder="Where is the police station?" maxlength="200" required></div></div>
          <button class="btn ghost" type="submit">Translate with AI</button></form><div id="tr-out"></div></div>

      <div class="card span-2"><h2>🧭 Travel guide</h2>
        ${prefs && !S.editPrefs ? `<p class="sub">Personalised for <b>${esc(prefs.interests)}</b> · ${esc(prefs.budget_level)} budget · ${esc(prefs.pace)} pace
            <button class="link-btn" data-act="edit-prefs">Edit</button></p>
          <form class="chat-form" id="f-explore"><input class="input" name="query" placeholder="e.g. best street food nearby, hidden gems, what to see today" maxlength="300" required>
            <button class="btn" type="submit">Explore</button></form><div id="explore-out"></div>`
        : `<p class="sub">Tell us your travel style once, then ask for recommendations anytime.</p>
          <form class="form" id="f-prefs"><div class="field"><label>Interests</label><input class="input" name="interests" required maxlength="120"
              value="${esc(prefs?.interests || "")}" placeholder="e.g. food, history, nature"></div>
            <div class="two"><div class="field"><label>Budget</label><select name="budget_level">${["low", "medium", "high"].map(b => `<option ${prefs?.budget_level === b ? "selected" : ""}>${b}</option>`).join("")}</select></div>
              <div class="field"><label>Pace</label><select name="pace">${["relaxed", "packed"].map(p => `<option ${prefs?.pace === p ? "selected" : ""}>${p}</option>`).join("")}</select></div></div>
            <button class="btn" type="submit">Save preferences</button></form>`}</div>
    </div>`;
  };

  V.id = () => {
    const d = S.ov.digital_id;
    if (d) return `
    <div class="page-title"><div><h1>🪪 Digital Tourist ID</h1><p>Sealed on a tamper-proof ledger. Police, hotels and guides can scan the QR to verify it.</p></div></div>
    <div class="grid g-2">
      <div class="idc"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px"><span class="eyebrow">Travel Buddy ID</span><span class="badge ok">ACTIVE</span></div>
        <div class="nm">${esc(d.full_name)}</div><div class="tid">${esc(d.tourist_id)}</div>
        <dl><div><dt>Nationality</dt><dd>${esc(d.nationality)}</dd></div><div><dt>Document</dt><dd>${esc(d.doc_type)} ••••${esc(d.doc_last4)}</dd></div>
          <div><dt>Valid from</dt><dd>${esc(d.valid_from.slice(0, 10))}</dd></div><div><dt>Valid until</dt><dd>${esc(d.valid_until.slice(0, 10))}</dd></div>
          <div><dt>Emergency contact</dt><dd>${esc(d.emergency_name)} ${esc(d.emergency_phone)}</dd></div></dl>
        <p class="muted" style="font-size:12px;margin:16px 0 0">⛓️ <code>${esc(d.block_hash.slice(0, 40))}…</code></p></div>
      <div class="card qr"><img src="/api/me/id/qr.png?v=${encodeURIComponent(d.tourist_id)}" alt="QR code for ${esc(d.tourist_id)}" width="210" height="210">
        <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center"><a class="btn sm" href="/verify/${encodeURIComponent(d.tourist_id)}" target="_blank" rel="noopener">Open verification page</a>
          <button class="btn ghost sm" data-act="id-verify">Re-verify now</button><button class="btn ghost sm" data-act="id-revoke">Revoke</button></div></div>
    </div>`;
    return `
    <div class="page-title"><div><h1>🪪 Get your Digital Tourist ID</h1><p>Takes a minute. Your document number is never stored, only a salted hash.</p></div></div>
    <div class="card"><form class="form" id="f-id">
      <div class="two"><div class="field"><label>Full name (as on your ID)</label><input class="input" name="full_name" required minlength="2" maxlength="80"></div>
        <div class="field"><label>Nationality</label><input class="input" name="nationality" required maxlength="40" placeholder="e.g. Indian"></div></div>
      <div class="two"><div class="field"><label>Document type</label><select name="doc_type"><option>Passport</option><option>Aadhaar</option><option>Driving Licence</option><option>Other ID</option></select></div>
        <div class="field"><label>Document number 🔒</label><input class="input" name="doc_number" required autocomplete="off" pattern="[A-Za-z0-9\\-\\s]{4,30}" placeholder="hashed immediately, never saved"></div></div>
      <div class="two"><div class="field"><label>Emergency contact name</label><input class="input" name="emergency_name" required maxlength="60"></div>
        <div class="field"><label>Emergency contact phone</label><input class="input" name="emergency_phone" required placeholder="+91 98765 43210"></div></div>
      <div class="field"><label>Trip length (ID validity)</label><select name="days"><option value="3">3 days</option><option value="7" selected>7 days</option><option value="15">15 days</option><option value="30">30 days</option><option value="90">90 days</option></select></div>
      <button class="btn" type="submit">⛏️ Issue my Digital ID</button></form></div>`;
  };

  V.guardians = () => {
    const g = S.ov.guardians;
    return `
    <div class="page-title"><div><h1>👨‍👩‍👧 Guardians</h1><p>Family or friends who get your SOS, restricted-zone and missed check-in alerts on Telegram and in their own portal.</p></div></div>
    <div class="grid g-2">
      <div class="card"><h2>Invite a guardian</h2><p class="sub">They open the link in Telegram and tap START. The link works once.</p>
        <form class="form" id="f-guard"><div class="field"><label>Their name</label><input class="input" name="name" required maxlength="60" placeholder="e.g. Mum"></div>
          <button class="btn" type="submit">Create invite link</button></form><div id="invite-out"></div></div>
      <div class="card"><h2>Your guardians</h2><div class="list">${g.length ? g.map(x => `
        <div class="row"><div class="grow"><div class="t">${esc(x.name || "Guardian")}</div><div class="s">${x.linked ? "✅ linked — receives your alerts" : "⏳ invite not accepted yet"}</div></div>
          <div class="acts"><button class="btn ghost xs" data-act="guard-remove" data-id="${x.id}">Remove</button></div></div>`).join("")
        : '<div class="empty"><span class="e">🫂</span>No guardians yet. Invite someone you trust.</div>'}</div></div>
    </div>`;
  };

  V.family = () => {
    if (!S.family) return '<div class="p-loading"><div class="spinner"></div>Loading your family…</div>';
    if (!S.family.length) return '<div class="empty"><span class="e">👪</span>You are not a guardian for anyone yet.</div>';
    return `<div class="page-title"><div><h1>👪 People you protect</h1><p>Live status of travellers who linked you as their guardian. Updates every 20 seconds.</p></div></div>
    <div class="grid">${S.family.map(p => {
      const open = p.incidents.filter(i => i.status !== "resolved");
      const color = SCORE_COLOR[p.score.label] || "#4cc9f0";
      return `<div class="card">
        ${p.checkin_pending ? '<div class="alert-bar">⏳<b>Missed safety check-in — they haven\'t answered yet.</b></div>' : ""}
        ${open.some(i => i.severity === "critical") ? '<div class="alert-bar">🆘<b>Active SOS — act now.</b></div>' : ""}
        <div class="person"><div>
          <h2>${esc(p.name)}</h2><p class="sub">${p.tourist_id ? esc(p.tourist_id) + " · " : ""}${p.place ? "📍 " + esc(p.place) : p.location ? `📍 ${p.location.lat.toFixed(4)}, ${p.location.lng.toFixed(4)}` : "no location yet"}</p>
          <dl class="kv"><dt>Safety</dt><dd style="color:${color}">${p.score.score}/100 · ${esc(p.score.label)}</dd>
            <dt>Last seen</dt><dd>${p.location ? ago(p.location.seen_ts) : "—"}</dd>
            <dt>Zone</dt><dd style="color:${RISK[p.zone_risk]}">${p.zones_inside.length ? esc(p.zones_inside.join(", ")) : "no risk zone"}</dd>
            <dt>Trip</dt><dd>${p.trip ? esc(p.trip.destination) + " (" + esc(p.trip.status) + ")" : "none"}</dd>
            <dt>Timer</dt><dd>${p.timer_due_ts ? "due in " + Math.max(0, Math.round((p.timer_due_ts - Date.now() / 1000) / 60)) + " min" : "off"}</dd></dl>
          <div class="list">${open.length ? open.map(i => `<div class="row sev-${i.severity}"><div class="grow"><div class="t">#${i.id} ${esc(i.label)} <span class="pill ${i.status}">${i.status}</span></div>
            <div class="s">${isoAgo(i.created_at)}</div>${i.description ? `<div class="d">${esc(i.description)}</div>` : ""}</div>
            <div class="acts">${i.status === "open" ? `<button class="btn ghost xs" data-act="ack" data-id="${i.id}">👀 On it</button>` : ""}<button class="btn ok xs" data-act="resolve" data-id="${i.id}">Resolve</button>
            <a class="btn ghost xs" href="/api/incident/${i.id}/efir">E-FIR</a></div></div>`).join("") : '<p class="muted" style="font-size:13px">No open incidents. 🙌</p>'}</div>
        </div><div class="map" id="map-fam-${p.id}"></div></div></div>`;
    }).join("")}</div>`;
  };

  V.settings = () => {
    const ov = S.ov;
    return `
    <div class="page-title"><div><h1>⚙️ Settings</h1><p>Same settings as in the Telegram bot.</p></div></div>
    <div class="grid g-2">
      <div class="card"><h2>Protection</h2>
        <div class="switch"><div><b>Automatic monitoring</b><span>Detects sudden halts, long stops, route deviation and lost signal during trips.</span></div>
          <label class="toggle"><input type="checkbox" data-set="monitoring" ${ov.monitoring ? "checked" : ""}><i></i></label></div>
        <div class="switch"><div><b>Community alerts</b><span>Anonymous heads-up when travellers report incidents within 2 km of you.</span></div>
          <label class="toggle"><input type="checkbox" data-set="community" ${ov.community ? "checked" : ""}><i></i></label></div></div>
      <div class="card"><h2>⏱️ Safety timer</h2><p class="sub">Going on a hike or a night out? If you don't check in before it runs out, we check on you and then alert your guardians.</p>
        ${ov.timer_due_ts ? `<div class="note">⏱️ Timer due in <b>${Math.max(0, Math.round((ov.timer_due_ts - Date.now() / 1000) / 60))} min</b>.</div>` : ""}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">${[30, 60, 90, 120, 240].map(m => `<button class="btn ghost sm" data-act="timer" data-min="${m}">${m < 60 ? m + " min" : m / 60 + " h"}</button>`).join("")}
          ${ov.timer_due_ts ? '<button class="btn danger sm" data-act="timer" data-min="0">Cancel timer</button>' : ""}</div></div>
    </div>`;
  };

  // ───────────── render ─────────────
  function render() {
    Object.values(S.maps).forEach(m => m.remove()); S.maps = {};
    const view = $("#view");
    if (!S.ov) return;
    view.innerHTML = (V[S.tab] || V.home)();
    view.style.animation = "none"; void view.offsetWidth; view.style.animation = "";
    after();
  }

  function after() {
    // score ring + count-up
    const fg = $(".ring .fg");
    if (fg) requestAnimationFrame(() => { fg.style.strokeDashoffset = 427 * (1 - (+fg.dataset.score) / 100); });
    $$("[data-count]").forEach(el => {
      const to = +el.dataset.count, t0 = performance.now();
      const step = n => { const p = Math.min(1, (n - t0) / 900); el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3))); if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
    if ($("#map-home")) drawSafetyMap(makeMap("map-home"), S.ov);
    if ($("#map-trip")) drawSafetyMap(makeMap("map-trip"), S.ov);
    (S.family || []).forEach(p => {
      if (!$("#map-fam-" + p.id)) return;
      const m = makeMap("map-fam-" + p.id);
      if (p.trail.length > 1) L.polyline(p.trail.map(t => [t.lat, t.lng]), { color: "#4cc9f0", weight: 3, opacity: .7, dashArray: "4 6" }).addTo(m);
      if (p.location) { L.marker([p.location.lat, p.location.lng], { icon: pin(p.checkin_pending ? "alert" : "me") }).bindPopup(esc(p.name)).addTo(m); m.setView([p.location.lat, p.location.lng], 14); }
    });
    bindSOS();
    bindForms();
    bindAssistant();
  }

  // ───────────── actions ─────────────
  async function refresh(silent = true) {
    if (S.loading) return;
    S.loading = true;
    try {
      S.ov = await api("GET", "/api/me/overview");
      if (S.me.guarding && (S.tab === "family" || !S.family)) S.family = (await api("GET", "/api/family")).people;
      // Only redraw when something changed, so maps keep their zoom and forms keep their input.
      const sig = JSON.stringify([{ ...S.ov, now: 0 }, S.family]);
      const changed = sig !== S.sig;
      S.sig = sig;
      const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      if (!silent || (changed && (S.tab === "home" || S.tab === "family") && !typing && !$("#dlg").open)) render();
      const badge = S.ov.incidents.filter(i => i.status !== "resolved").length;
      const tabBtn = $('#tabs button[data-tab="incidents"]');
      $(".badge-dot", tabBtn)?.remove();
      if (badge) tabBtn.insertAdjacentHTML("beforeend", `<span class="badge-dot">${badge}</span>`);
    } catch (e) { if (!silent) toast(esc(e.message), "bad"); }
    finally { S.loading = false; }
  }

  async function shareLocation(btn) {
    await run(btn, async () => {
      const pos = await getPosition();
      const res = await api("POST", "/api/me/location", pos);
      res.notes.forEach(n => { S.notes.unshift(n); toast(n, n.includes("🔔") || n.includes("restricted") ? "warn" : "info"); });
      S.notes = S.notes.slice(0, 8);
      if (!res.notes.length) toast("📍 Location updated. No alerts here.", "ok");
      await refresh(false);
    });
  }

  async function sendSOS() {
    toast("🆘 Sending SOS…", "bad");
    let pos = null;
    try { pos = await getPosition(4000); } catch { /* send with last known location */ }
    try {
      const r = await api("POST", "/api/me/sos", pos || {});
      const nums = Object.entries(r.emergency_numbers).map(([k, v]) => `<a href="tel:${esc(String(v).split("/")[0].trim())}">${esc(k)}<b>${esc(v)}</b></a>`).join("");
      const near = r.nearby.map(s => `<a href="https://www.google.com/maps?q=${s.lat},${s.lng}" target="_blank" rel="noopener">${s.type === "police" ? "👮" : "🏥"} ${esc(s.name)}<b>${(s.distance_m / 1000).toFixed(1)} km</b></a>`).join("");
      await refresh(false);
      const choice = await dialog(`<h2>🆘 SOS sent — incident #${r.incident.id}</h2>
        <p>${r.delivered ? `✅ <b>${r.delivered}</b> guardian(s)/responder(s) alerted with your location.` : "⚠️ No guardian or safety desk could be reached. <b>Call an emergency number now.</b>"}</p>
        <p class="muted">${r.fresh ? "📍 Your current location was attached." : "📍 Location could not be updated. Share it as soon as you can."}</p>
        <div class="numbers">${nums}</div>${near ? `<h3>Closest help</h3><div class="numbers">${near}</div>` : ""}`,
        [["I'm safe now", "ok"], ["Close", "ghost"]]);
      if (choice === 0) markSafe();
    } catch (e) { toast("SOS failed: " + esc(e.message) + " — call 112 now.", "bad"); }
  }

  async function markSafe(btn) {
    await run(btn, async () => { const r = await api("POST", "/api/me/safe"); toast("✅ " + esc(r.message.replace(/^✅\s*/, "")), "ok"); await refresh(false); });
  }

  function bindSOS() {
    const b = $("#sos");
    if (!b) return;
    let raf = null, t0 = 0;
    const stop = () => { cancelAnimationFrame(raf); raf = null; b.classList.remove("holding"); b.style.setProperty("--p", 0); };
    const tick = n => {
      const p = Math.min(100, (n - t0) / 15);
      b.style.setProperty("--p", p);
      if (p >= 100) { stop(); navigator.vibrate?.(200); sendSOS(); return; }
      raf = requestAnimationFrame(tick);
    };
    const start = e => { e.preventDefault(); if (raf) return; t0 = performance.now(); b.classList.add("holding"); raf = requestAnimationFrame(tick); };
    b.addEventListener("pointerdown", start);
    ["pointerup", "pointerleave", "pointercancel"].forEach(ev => b.addEventListener(ev, stop));
    b.addEventListener("keydown", e => { if ((e.key === "Enter" || e.key === " ") && !e.repeat) start(e); });
    b.addEventListener("keyup", stop);
  }

  async function loadAssistant() {
    try {
      const [ph, pr] = await Promise.all([api("GET", "/api/me/phrases"), api("GET", "/api/me/preferences")]);
      S.phrases = ph; S.prefs = pr.preferences; S.prefsLoaded = true;
      if (S.tab === "assistant") render();
    } catch (e) { toast(esc(e.message), "bad"); }
  }

  function pushChat(who, html) {
    S.chat.push({ who, html });
    S.chat = S.chat.slice(-30);
    const box = $("#chat");
    if (box) { box.innerHTML = S.chat.map(m => `<div class="msg ${m.who}">${m.html}</div>`).join(""); box.scrollTop = box.scrollHeight; }
  }

  // Shrink photos in the browser before upload (keeps uploads small and fast).
  const shrinkImage = (file, max = 1600) => new Promise((res, rej) => {
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      res(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => { URL.revokeObjectURL(url); rej(new Error("That file isn't a readable image.")); };
    img.src = url;
  });

  function bindAssistant() {
    const ask = $("#f-ask");
    if (ask) {
      $("#chat").scrollTop = 1e6;
      ask.addEventListener("submit", e => {
        e.preventDefault();
        const q = ask.question.value.trim(); if (!q) return;
        ask.question.value = "";
        pushChat("me", esc(q));
        pushChat("bot typing", "<span class=\"dots\"><i></i><i></i><i></i></span>");
        run(ask.querySelector("[type=submit]"), async () => {
          try {
            const r = await api("POST", "/api/me/ask", { question: q });
            S.chat.pop();
            pushChat(r.distress ? "bot alert" : "bot", r.html);
          } catch (err) { S.chat.pop(); pushChat("bot", "⚠️ " + esc(err.message)); }
        });
      });
    }
    const tips = $("#f-tips");
    if (tips) tips.addEventListener("submit", e => { e.preventDefault(); run(tips.querySelector("[type=submit]"), async () => {
      $("#tips-out").innerHTML = '<div class="p-loading"><div class="spinner"></div>Preparing briefing…</div>';
      const r = await api("POST", "/api/me/safetytips", formData(tips));
      $("#tips-out").innerHTML = `<div class="ai-out"><div class="muted" style="margin-bottom:6px">📍 ${esc(r.place)}</div>${r.html}</div>`;
    }).finally(() => { if ($("#tips-out .p-loading")) $("#tips-out").innerHTML = ""; }); });
    const lang = $("#lang");
    if (lang) lang.addEventListener("change", () => { S.lang = lang.value; render(); });
    const tr = $("#f-translate");
    if (tr) tr.addEventListener("submit", e => { e.preventDefault(); run(tr.querySelector("[type=submit]"), async () => {
      const r = await api("POST", "/api/me/phrases/translate", formData(tr));
      $("#tr-out").innerHTML = `<div class="phrase big"><span>${esc(r.text)} → ${esc(r.language)}</span><b>${esc(r.result)}</b></div>`;
    }); });
    const prefs = $("#f-prefs");
    if (prefs) prefs.addEventListener("submit", e => { e.preventDefault(); run(prefs.querySelector("[type=submit]"), async () => {
      const r = await api("POST", "/api/me/preferences", formData(prefs));
      S.prefs = r.preferences; S.editPrefs = false; toast("✅ Preferences saved.", "ok"); render();
    }); });
    const ex = $("#f-explore");
    if (ex) ex.addEventListener("submit", e => { e.preventDefault(); run(ex.querySelector("[type=submit]"), async () => {
      $("#explore-out").innerHTML = '<div class="p-loading"><div class="spinner"></div>Searching fresh local info…</div>';
      try {
        const r = await api("POST", "/api/me/explore", formData(ex));
        $("#explore-out").innerHTML = `<div class="ai-out">${r.context ? `<div class="muted" style="margin-bottom:6px">📍 Guide for ${esc(r.context)}</div>` : ""}${r.html}</div>`;
      } catch (err) { $("#explore-out").innerHTML = ""; throw err; }
    }); });
    const ev = $("#f-evidence");
    if (ev) ev.addEventListener("submit", e => { e.preventDefault(); run(ev.querySelector("[type=submit]"), async () => {
      const file = ev.photo.files[0];
      if (!file) throw new Error("Choose a photo first.");
      const image_b64 = await shrinkImage(file);
      const r = await api("POST", "/api/me/evidence", { image_b64, caption: ev.caption.value, incident_id: ev.incident_id.value || null });
      await refresh(false);
      dialog(`<h2>🔐 Evidence sealed</h2><p>${r.created ? "New incident" : "Attached to incident"} <b>#${r.incident.id}</b> · ${esc(r.incident.label)}</p>
        <div class="note">SHA-256 <code>${esc(r.sha256.slice(0, 32))}…</code><br>Sealed in ledger block #${r.block_index} · sent to ${r.forwarded} responder(s)</div>
        <p class="muted" style="margin-top:12px">This fingerprint proves the photo hasn't been altered: useful for the police and insurance.</p>`,
        [["Download E-FIR", ""], ["Close", "ghost"]]).then(i => { if (i === 0) location.href = `/api/incident/${r.incident.id}/efir`; });
    }); });
  }

  function formData(f) { const d = Object.fromEntries(new FormData(f)); $$("input[type=checkbox]", f).forEach(c => d[c.name] = c.checked); return d; }

  function bindForms() {
    const trip = $("#f-trip");
    if (trip) {
      let mode = "train";
      $("#mode").addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; mode = b.dataset.v; $$("#mode button").forEach(x => x.classList.toggle("on", x === b)); });
      trip.addEventListener("submit", e => { e.preventDefault(); run(trip.querySelector("[type=submit]"), async () => {
        const r = await api("POST", "/api/me/trip", { ...formData(trip), mode });
        toast(`🚆 Trip started → ${esc(r.resolved_as.slice(0, 80))}. Turn on live tracking or share your location.`, "ok");
        await refresh(false);
      }); });
    }
    const route = $("#f-route");
    if (route) route.addEventListener("submit", e => { e.preventDefault(); run(route.querySelector("[type=submit]"), async () => {
      const r = await api("POST", "/api/me/saferoute", formData(route));
      const color = r.score >= 75 ? "#2ee59d" : r.score >= 50 ? "#ffc857" : "#ff4d6d";
      $("#route-out").innerHTML = `<div class="note" style="margin-top:14px"><b style="color:${color};font-size:18px">${r.score}/100</b> · ${esc(r.verdict)}<br>
        <span class="muted">${r.distance_km} km · ~${r.duration_min} min${r.source !== "road" ? " (straight-line estimate)" : ""}${r.weather ? " · " + esc(r.weather) : ""}</span>
        ${r.reasons.length ? `<ul class="factors">${r.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        <p style="margin:10px 0 0"><a href="${esc(r.directions)}" target="_blank" rel="noopener">Open directions →</a></p></div><div class="map" id="map-route" style="margin-top:12px;height:260px"></div>`;
      const m = makeMap("map-route");
      const line = L.polyline(r.path, { color, weight: 5, opacity: .85 }).addTo(m);
      m.fitBounds(line.getBounds(), { padding: [30, 30] });
    }); });
    const report = $("#f-report");
    if (report) report.addEventListener("submit", e => { e.preventDefault(); run(report.querySelector("[type=submit]"), async () => {
      const r = await api("POST", "/api/me/report", formData(report));
      await refresh(false);
      await dialog(`<h2>✅ Incident #${r.incident.id} recorded</h2><p><span class="pill ${r.incident.severity}">${r.incident.severity}</span> ${esc(r.incident.label)}
        <span class="muted">(AI triage: ${esc(r.triage.engine)})</span></p><p>${r.delivered ? `📣 ${r.delivered} responder(s) alerted.` : "Logged."}</p>
        <div class="note">💡 ${esc(r.triage.advice)}</div>`, [["Download E-FIR", ""], ["Close", "ghost"]]).then(i => { if (i === 0) location.href = `/api/incident/${r.incident.id}/efir`; });
    }); });
    const idf = $("#f-id");
    if (idf) idf.addEventListener("submit", e => { e.preventDefault(); run(idf.querySelector("[type=submit]"), async () => {
      await api("POST", "/api/me/id", formData(idf));
      toast("🪪 Digital ID issued and sealed on the ledger!", "ok");
      S.me = await api("GET", "/api/me"); renderHeader();
      await refresh(false);
    }); });
    const gf = $("#f-guard");
    if (gf) gf.addEventListener("submit", e => { e.preventDefault(); run(gf.querySelector("[type=submit]"), async () => {
      const r = await api("POST", "/api/me/guardians", formData(gf));
      $("#invite-out").innerHTML = `<div class="note" style="margin-top:14px">Send this link to them:<div class="copybox"><input class="input" readonly value="${esc(r.link)}"><button class="btn sm" type="button" data-act="copy">Copy</button></div></div>`;
      S.ov = await api("GET", "/api/me/overview");
    }); });
    $$("[data-set]").forEach(c => c.addEventListener("change", () => run(null, async () => {
      await api("POST", "/api/me/settings", { [c.dataset.set]: c.checked });
      toast(`Saved: ${c.dataset.set} ${c.checked ? "on" : "off"}.`, "ok");
      await refresh(true);
    })));
    const live = $("#live");
    if (live) live.addEventListener("change", () => live.checked ? startLive() : stopLive());
  }

  // ───────────── live tracking ─────────────
  function startLive() {
    if (!navigator.geolocation) { toast("Location isn't available in this browser.", "bad"); return; }
    S.watch = navigator.geolocation.watchPosition(async p => {
      if (Date.now() - S.lastSent < 20000) return;
      S.lastSent = Date.now();
      try {
        const res = await api("POST", "/api/me/location", { lat: p.coords.latitude, lng: p.coords.longitude });
        res.notes.forEach(n => { S.notes.unshift(n); toast(n, n.includes("🔔") ? "warn" : "info"); });
        S.notes = S.notes.slice(0, 8);
        await refresh(true);
      } catch (e) { toast(esc(e.message), "bad"); }
    }, err => { toast(err.code === 1 ? "Location permission denied." : "Location unavailable.", "bad"); stopLive(); },
    { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 });
    toast("📡 Live tracking on. Keep this page open.", "ok");
  }
  function stopLive() { if (S.watch !== null) navigator.geolocation.clearWatch(S.watch); S.watch = null; const l = $("#live"); if (l) l.checked = false; }

  // delegated buttons
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const act = b.dataset.act, id = b.dataset.id;
    if (act === "safe") markSafe(b);
    else if (act === "locate") shareLocation(b);
    else if (act === "nearby") run(b, async () => {
      const r = await api("GET", "/api/me/nearby");
      const list = r.services.map(s => `<a href="${esc(s.map)}" target="_blank" rel="noopener">${({ police: "👮", hospital: "🏥", clinic: "🩺", pharmacy: "💊", fire_station: "🚒" })[s.type] || "📍"} ${esc(s.name)}<b>${(s.distance_m / 1000).toFixed(1)} km</b></a>`).join("");
      const nums = Object.entries(r.emergency_numbers).map(([k, v]) => `<a href="tel:${esc(String(v).split("/")[0].trim())}">${esc(k)}<b>${esc(v)}</b></a>`).join("");
      dialog(`<h2>🏥 Nearest help</h2><div class="numbers">${list || '<p class="muted">No services found nearby right now.</p>'}</div><h3>☎️ Emergency numbers${r.country ? " (" + esc(r.country.toUpperCase()) + ")" : ""}</h3><div class="numbers">${nums}</div>`);
    });
    else if (act === "resolve" || act === "ack") run(b, async () => {
      await api("POST", `/api/incident/${id}/${act === "ack" ? "ack" : "resolve"}`);
      toast(act === "ack" ? `👀 You acknowledged incident #${id}. They've been told you're on it.` : `🟢 Incident #${id} resolved.`, "ok");
      if (S.tab === "family") S.family = (await api("GET", "/api/family")).people;
      await refresh(false);
    });
    else if (act === "trip-end" || act === "trip-cancel") run(b, async () => {
      await api("POST", act === "trip-end" ? "/api/me/trip/end" : "/api/me/trip/cancel"); stopLive();
      toast(act === "trip-end" ? "🏁 Trip completed. Welcome!" : "Trip cancelled.", "ok"); await refresh(false);
    });
    else if (act === "timer") run(b, async () => {
      const m = +b.dataset.min; await api("POST", "/api/me/timer", { minutes: m });
      toast(m ? `⏱️ Safety timer set for ${m} min.` : "Timer cancelled.", "ok"); await refresh(false);
    });
    else if (act === "id-verify") run(b, async () => {
      const r = await api("POST", "/api/me/id/verify");
      toast(r.valid ? "✅ Verified: record and ledger chain are intact." : "⚠️ " + esc(r.reasons.join("; ")), r.valid ? "ok" : "bad");
    });
    else if (act === "id-revoke") dialog("<h2>Revoke your Digital ID?</h2><p>The revocation is recorded on the ledger. You can issue a new ID afterwards.</p>",
      [["Revoke", "danger"], ["Keep it", "ghost"]]).then(i => { if (i === 0) run(null, async () => { await api("POST", "/api/me/id/revoke"); toast("ID revoked.", "ok"); S.me = await api("GET", "/api/me"); renderHeader(); await refresh(false); }); });
    else if (act === "guard-remove") run(b, async () => { await api("POST", `/api/me/guardians/${id}/remove`); toast("Guardian removed.", "ok"); await refresh(false); });
    else if (act === "edit-prefs") { S.editPrefs = true; render(); }
    else if (act === "copy") { const i = b.previousElementSibling; i.select(); navigator.clipboard?.writeText(i.value); b.textContent = "Copied ✓"; }
  });

  // ───────────── boot ─────────────
  (async () => {
    try {
      S.me = await api("GET", "/api/me");
      renderHeader();
      const want = (location.hash || "#home").slice(1);
      S.ov = await api("GET", "/api/me/overview");
      if (S.me.guarding) S.family = (await api("GET", "/api/family")).people;
      setTab(["home", "trip", "incidents", "assistant", "id", "guardians", "family", "settings"].includes(want) ? want : "home", false);
    } catch (e) {
      $("#view").innerHTML = `<div class="empty"><span class="e">⚠️</span>${esc(e.message)}</div>`;
    }
    setInterval(() => { if (!document.hidden) refresh(true); }, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  })();
})();
