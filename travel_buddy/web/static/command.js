/* Travel Buddy — Command Center client
   Polls /api/overview, diffs state and animates changes (new incidents, KPI changes, alarms). */
(() => {
  "use strict";
  const TOKEN = window.TB_TOKEN;
  const REFRESH_MS = 10000;
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const RISK = { safe: "#2ee59d", caution: "#ffc857", high: "#ff8c42", restricted: "#ff4d6d" };
  const SEV = { critical: "#ff4d6d", high: "#ff8c42", medium: "#ffc857", low: "#4cc9f0" };
  const TYPE = { sos: "SOS alert", medical: "Medical", theft: "Theft / loss", harassment: "Harassment", accident: "Accident",
    lost: "Lost / missing", scam: "Scam / fraud", natural_hazard: "Natural hazard", other: "Other",
    geofence_breach: "Restricted-zone entry", no_response: "No check-in response" };
  const ICON = {
    users: '<circle cx="9" cy="8" r="4"/><path d="M2 21c0-4 3-7 7-7s7 3 7 7"/><path d="M16 4a4 4 0 0 1 0 8M22 21c0-3-2-5.5-5-6.5"/>',
    route: '<circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/><path d="M6 16V9a4 4 0 0 1 4-4h5M18 8v7a4 4 0 0 1-4 4H9"/>',
    alert: '<path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/>',
    siren: '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 21h14M12 3v2M4.2 7.2l1.4 1.4M19.8 7.2l-1.4 1.4"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    shield: '<path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z"/><path d="M12 8v4M12 16h.01"/>',
    flame: '<path d="M12 22c4 0 7-3 7-7 0-5-4-7-5-12-2 3-3 5-3 7-1-1-2-2-2-4-2 2-4 5-4 9 0 4 3 7 7 7z"/>',
    id: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="12" r="2.5"/><path d="M14 10h4M14 14h4"/>',
  };
  const svg = p => `<svg viewBox="0 0 24 24">${p}</svg>`;
  const KPIS = [
    ["live_tourists", "Live tourists", "users", "#4cc9f0"],
    ["active_trips", "Active trips", "route", "#7b61ff"],
    ["open_incidents", "Open incidents", "alert", "#ff8c42"],
    ["critical", "Critical", "siren", "#ff4d6d"],
    ["pending_checkins", "Pending check-ins", "clock", "#ffc857"],
    ["in_risk_zones", "In risk zones", "shield", "#ff8c42"],
    ["hotspots", "Hotspots", "flame", "#ff4d9d"],
    ["digital_ids", "Digital IDs", "id", "#2ee59d"],
  ];

  // ---------- state ----------
  const state = { data: null, tab: "incidents", sev: "all", q: "", selected: null, first: true,
    knownInc: new Set(), prevStats: {}, sound: false, zoneSig: "", hotSig: "" };

  // ---------- KPI strip ----------
  $("#kpis").innerHTML = KPIS.map(([k, label, ic, c], i) =>
    `<div class="kpi" id="k-${k}" style="--kc:${c};animation-delay:${i * 45}ms"><div class="ico">${svg(ICON[ic])}</div>
     <div><b data-v="0">0</b><span>${label}</span></div></div>`).join("");

  const animateNumber = (el, to) => {
    const from = Number(el.dataset.v) || 0;
    el.dataset.v = to;
    if (from === to) { el.textContent = to; return; }
    const start = performance.now(), dur = 700;
    const step = now => {
      const p = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  // ---------- map ----------
  const map = L.map("map", { zoomControl: false, attributionControl: true, preferCanvas: false }).setView([22.5, 82], 5);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19, className: "dark-tiles",
  }).addTo(map);
  // Resizing in the middle of a fly animation desyncs Leaflet's tile grid; settle first.
  let resizeTimer;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { map.stop(); map.invalidateSize({ pan: false }); }, 150);
  }).observe($("#map"));
  const layers = { zones: L.layerGroup().addTo(map), hot: L.layerGroup().addTo(map),
                   tour: L.layerGroup().addTo(map), inc: L.layerGroup().addTo(map) };
  L.control.layers(null, { "Geo-fence zones": layers.zones, "Learned hotspots": layers.hot,
    "Tourists": layers.tour, "Incidents": layers.inc }, { position: "topright" }).addTo(map);
  const markers = { tour: new Map(), inc: new Map() };

  const icon = (cls, rings) => L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11],
    html: `<div class="mk ${cls}"><div class="core"></div>${'<div class="ring"></div>'.repeat(rings)}</div>` });

  const syncMarkers = (kind, items, keyFn, buildIcon, popup, zKey) => {
    const seen = new Set();
    items.forEach(it => {
      if (it.lat == null) return;
      const key = keyFn(it); seen.add(key);
      let m = markers[kind].get(key);
      const ic = buildIcon(it);
      if (!m) {
        m = L.marker([it.lat, it.lng], { icon: ic, zIndexOffset: zKey ? zKey(it) : 0, riseOnHover: true })
          .addTo(layers[kind]).bindPopup(popup(it), { maxWidth: 280 });
        m.on("click", () => select(kind, key, false));
        markers[kind].set(key, m);
      } else {
        m.setLatLng([it.lat, it.lng]);
        if (m._tbSig !== ic.options.html) m.setIcon(ic);  // keep pulse animations running
        m.setPopupContent(popup(it));
      }
      m._tbSig = ic.options.html;
    });
    for (const [key, m] of markers[kind]) if (!seen.has(key)) { layers[kind].removeLayer(m); markers[kind].delete(key); }
  };

  const ago = iso => {
    if (!iso) return "";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    const s = Math.max(0, (Date.now() - d) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return d.toLocaleDateString();
  };

  const incPopup = i => `<div class="pop-title">#${i.incident_id} · ${esc(TYPE[i.incident_type] || i.incident_type)}</div>
    <div><span class="sev ${i.severity}">${i.severity}</span> <span class="st ${i.status}">${i.status}</span></div>
    <div style="margin-top:8px">${esc(i.description || "")}</div>
    <div class="pop-meta" style="margin-top:8px">${esc(i.name)}${i.tid ? " · " + esc(i.tid) : ""}<br>${ago(i.created_at)}</div>`;
  const tourPopup = t => `<div class="pop-title">${esc(t.name)}</div>
    <div class="pop-meta">${t.tid ? esc(t.tid) + " · " : ""}seen ${t.minutes_ago} min ago</div>
    <div style="margin-top:6px">Zone risk: <b style="color:${RISK[t.zone_risk]}">${t.zone_risk}</b></div>
    ${t.checkin_pending ? '<div style="color:#ff4d6d;font-weight:700;margin-top:6px">⏳ Check-in pending</div>' : ""}`;

  const drawAreas = d => {
    const zs = JSON.stringify(d.zones.map(z => [z.zone_id, z.radius_m, z.risk_level]));
    if (zs !== state.zoneSig) {
      state.zoneSig = zs; layers.zones.clearLayers();
      d.zones.forEach(z => L.circle([z.center_lat, z.center_lng], { radius: z.radius_m, color: RISK[z.risk_level],
        weight: 1.5, fillOpacity: .08, className: "zone" })
        .bindPopup(`<div class="pop-title">${esc(z.name)}</div><div class="pop-meta">${z.risk_level} zone · ${(z.radius_m / 1000).toFixed(1)} km</div><div style="margin-top:6px">${esc(z.advisory || "")}</div>`)
        .addTo(layers.zones));
    }
    const hs = d.hotspots.filter(h => h.risk_level !== "safe");
    const hsig = JSON.stringify(hs.map(h => [h.lat.toFixed(4), h.lng.toFixed(4), h.count]));
    if (hsig !== state.hotSig) {
      state.hotSig = hsig; layers.hot.clearLayers();
      hs.forEach(h => L.circle([h.lat, h.lng], { radius: h.radius_m, color: RISK[h.risk_level], weight: 2, fillOpacity: .2, className: "hot-zone" })
        .bindPopup(`<div class="pop-title">🔥 Hotspot ${h.hotspot_id}</div><div class="pop-meta">${h.count} incidents · score ${h.score}</div><div style="margin-top:6px">Mostly <b>${esc(h.dominant_type)}</b></div>`)
        .addTo(layers.hot));
    }
  };

  const fitAll = () => {
    const pts = [];
    layers.tour.eachLayer(m => pts.push(m.getLatLng()));
    layers.inc.eachLayer(m => pts.push(m.getLatLng()));
    if (pts.length) map.flyToBounds(L.latLngBounds(pts), { padding: [60, 60], maxZoom: 13, duration: 1.2 });
  };

  // ---------- selection ----------
  function select(kind, key, fly = true) {
    state.selected = `${kind}:${key}`;
    ["tour", "inc"].forEach(k => markers[k].forEach((mk, id) =>
      mk.getElement()?.querySelector(".mk")?.classList.toggle("selected", k === kind && id === key)));
    const m = markers[kind].get(key);
    if (m && fly) { map.flyTo(m.getLatLng(), Math.max(map.getZoom(), 14), { duration: 1.1 }); setTimeout(() => m.openPopup(), 900); }
    else if (m) m.openPopup();
    if (kind === "inc" && state.tab !== "incidents") setTab("incidents");
    if (kind === "tour" && state.tab !== "tourists") setTab("tourists");
    renderList();
    document.querySelector(`[data-key="${state.selected}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // ---------- tabs & filters ----------
  const tabs = [...document.querySelectorAll(".tab")];
  const ind = $(".tab-ind");
  function setTab(name) {
    if (name !== state.tab) state.deskRendered = false;
    state.tab = name;
    tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === name));
    const a = tabs.find(t => t.dataset.tab === name);
    ind.style.width = a.offsetWidth + "px";
    ind.style.transform = `translateX(${a.offsetLeft - 6}px)`;
    $("#chips").style.display = name === "incidents" ? "" : "none";
    renderList();
  }
  tabs.forEach(t => t.addEventListener("click", () => setTab(t.dataset.tab)));
  window.addEventListener("resize", () => setTab(state.tab));

  $("#chips").innerHTML = ["all", "critical", "high", "medium", "low"].map(s =>
    `<button class="chip-f${s === "all" ? " on" : ""}" data-sev="${s}" style="--cc:${SEV[s] || "#4cc9f0"}">${s}</button>`).join("");
  $("#chips").addEventListener("click", e => {
    const b = e.target.closest(".chip-f"); if (!b) return;
    state.sev = b.dataset.sev;
    document.querySelectorAll(".chip-f").forEach(c => c.classList.toggle("on", c === b));
    renderList();
  });
  $("#search").addEventListener("input", e => { state.q = e.target.value.toLowerCase().trim(); renderList(); });

  // ---------- lists ----------
  const match = (...fields) => !state.q || fields.join(" ").toLowerCase().includes(state.q);
  const empty = (e, t) => `<div class="empty"><span class="e">${e}</span>${t}</div>`;

  // ---------- safety desk: zones + free-tier usage ----------
  const desk = async (method, url, data) => {
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json", "X-Requested-With": "TravelBuddy", "X-TB-Token": TOKEN },
                                 body: method === "GET" ? undefined : JSON.stringify(data || {}) });
    const body = await r.json().catch(() => ({ ok: false, error: "Unexpected response" }));
    if (!body.ok) throw new Error(body.error || "Failed");
    return body;
  };
  const USAGE = { ai: "🤖 AI (Groq)", weather: "🌦️ Weather", routing: "🧭 Routing", places: "🏥 Places" };
  let preview = null;
  async function renderDesk() {
    state.deskRendered = true;
    const list = $("#list");
    list.innerHTML = '<div class="skel"></div><div class="skel"></div>';
    try {
      const [u, z] = await Promise.all([desk("GET", "/api/desk/usage"), desk("GET", "/api/desk/zones")]);
      if (state.tab !== "desk") return;
      list.innerHTML = `<div class="desk">
        <h4>💸 Free-tier usage today</h4>${u.usage.map(x => `<div class="usage-row">${USAGE[x.service] || x.service} <span class="muted">· ${x.used}/${x.limit}</span>
          <div class="bar"><i style="width:${Math.min(100, x.used * 100 / x.limit)}%"></i></div></div>`).join("")}
        <h4>➕ Add a geo-fence zone</h4>
        <form class="zone-form" id="zone-form">
          <input name="name" placeholder="Zone name, e.g. Army Cantonment" maxlength="80" required>
          <div class="r2"><select name="risk"><option value="caution">🟡 caution</option><option value="high">🟠 high</option>
            <option value="restricted">🔴 restricted</option><option value="safe">🟢 safe</option></select>
            <input name="radius_m" type="number" min="50" max="200000" value="1000" title="radius in metres" required></div>
          <div class="r2"><input name="lat" placeholder="latitude" required><input name="lng" placeholder="longitude" required></div>
          <span class="hint-pick">👆 Click the map to set the centre</span>
          <input name="advisory" placeholder="Advice shown to tourists" maxlength="300">
          <button type="submit">Add zone</button></form>
        <h4>🗺️ Active zones (${z.zones.length})</h4>
        ${z.zones.map(zz => `<div class="item" data-zone="${zz.lat},${zz.lng}" style="--ic:${RISK[zz.risk]}">
          <div class="item-top"><b>${esc(zz.name)}</b><span class="r" style="color:${RISK[zz.risk]}">${zz.risk}</span></div>
          <div class="item-sub">${(zz.radius_m / 1000).toFixed(1)} km radius${zz.advisory ? " · " + esc(zz.advisory.slice(0, 80)) : ""}</div>
          <div class="item-acts"><button class="act" data-zone-remove="${zz.id}">🗑️ Remove</button></div></div>`).join("") || empty("🗺️", "No zones yet.")}
      </div>`;
      $("#zone-form").addEventListener("submit", async e => {
        e.preventDefault();
        const f = e.target, btn = f.querySelector("button");
        btn.disabled = true;
        try {
          const data = Object.fromEntries(new FormData(f));
          const r = await desk("POST", "/api/desk/zones", data);
          notify(`🗺️ Zone “${esc(r.zone.name)}” added. Tourists will be alerted on approach and entry.`, "#2ee59d");
          if (preview) { map.removeLayer(preview); preview = null; }
          state.zoneSig = ""; await refresh(); renderDesk();
        } catch (err) { notify("⚠️ " + esc(err.message), "#ff4d6d"); btn.disabled = false; }
      });
    } catch (err) { list.innerHTML = empty("⚠️", esc(err.message)); }
  }
  map.on("click", e => {
    if (state.tab !== "desk") return;
    const f = $("#zone-form"); if (!f) return;
    f.lat.value = e.latlng.lat.toFixed(5); f.lng.value = e.latlng.lng.toFixed(5);
    if (preview) map.removeLayer(preview);
    preview = L.circle(e.latlng, { radius: +f.radius_m.value || 1000, color: RISK[f.risk.value], dashArray: "4 4", fillOpacity: .15 }).addTo(map);
  });

  function renderList() {
    const d = state.data; if (!d) return;
    const list = $("#list");
    if (state.tab === "desk") { if (!state.deskRendered) renderDesk(); return; }  // form: don't redraw on refresh
    const open = d.incidents.filter(i => i.status !== "resolved");
    let html = "";
    if (state.tab === "incidents") {
      const items = open.filter(i => (state.sev === "all" || i.severity === state.sev)
        && match(i.name, i.tid, i.incident_type, i.description, "#" + i.incident_id));
      html = items.length ? items.map((i, n) => {
        const key = `inc:${i.incident_id}`;
        return `<div class="item sev-${i.severity}${state.selected === key ? " sel" : ""}${i._fresh ? " fresh" : ""}" data-key="${key}" data-kind="inc" data-id="${i.incident_id}" style="animation-delay:${Math.min(n, 10) * 35}ms">
          <div class="item-top"><b>#${i.incident_id}</b> ${esc(TYPE[i.incident_type] || i.incident_type)} <span class="sev ${i.severity}">${i.severity}</span><span class="r">${ago(i.created_at)}</span></div>
          <div class="item-sub">${esc(i.name)}${i.tid ? " · " + esc(i.tid) : ""} · <span class="st ${i.status}">${i.status}</span>${i.lat == null ? " · no location" : ""}</div>
          ${i.description ? `<div class="item-desc">${esc(i.description.slice(0, 140))}</div>` : ""}
          <div class="item-acts">${i.status === "open" ? `<button class="act ack" data-act="ack" data-id="${i.incident_id}">👀 Acknowledge</button>` : ""}
            <button class="act res" data-act="resolve" data-id="${i.incident_id}">✅ Resolve</button></div></div>`;
      }).join("") : empty("🛡️", state.q || state.sev !== "all" ? "No incidents match these filters." : "No open incidents. All quiet.");
    } else if (state.tab === "tourists") {
      const items = d.tourists.filter(t => match(t.name, t.tid, t.zone_risk))
        .sort((a, b) => (b.checkin_pending - a.checkin_pending) || (a.minutes_ago - b.minutes_ago));
      html = items.length ? items.map((t, n) => {
        const key = `tour:${t.id}`;
        return `<div class="item${t.checkin_pending ? " sev-critical" : ""}${state.selected === key ? " sel" : ""}" data-key="${key}" data-kind="tour" data-id="${t.id}" style="animation-delay:${Math.min(n, 10) * 35}ms">
          <div class="item-top"><b>${esc(t.name)}</b><span class="r">${t.minutes_ago} min ago</span></div>
          <div class="item-sub">${t.tid ? esc(t.tid) + " · " : ""}<span style="color:${RISK[t.zone_risk]}">● ${t.zone_risk} zone</span>${t.checkin_pending ? ' · <b style="color:#ff4d6d">check-in pending</b>' : ""}</div></div>`;
      }).join("") : empty("🧭", "No tourists have shared a location in the last 24 h.");
    } else if (state.tab === "hotspots") {
      const items = d.hotspots.filter(h => h.risk_level !== "safe" && match(h.dominant_type, h.hotspot_id));
      html = items.length ? items.map((h, n) => `<div class="item" data-hot="${h.lat},${h.lng}" style="--ic:${RISK[h.risk_level]};animation-delay:${n * 35}ms">
          <div class="item-top"><b>🔥 ${h.hotspot_id}</b> ${h.count} incidents <span class="r" style="color:${RISK[h.risk_level]}">${h.risk_level}</span></div>
          <div class="item-sub">Mostly ${esc(h.dominant_type)} · risk score ${h.score}</div></div>`).join("")
        : empty("🔥", "No hotspots yet. They appear automatically once incidents cluster.");
    } else {
      const ev = [];
      d.incidents.forEach(i => {
        ev.push({ t: i.created_at, c: SEV[i.severity], h: `<b>${esc(TYPE[i.incident_type] || i.incident_type)}</b> raised by ${esc(i.name)} <span class="sev ${i.severity}">${i.severity}</span>` });
        if (i.resolved_at) ev.push({ t: i.resolved_at, c: "#2ee59d", h: `Incident <b>#${i.incident_id}</b> resolved` });
      });
      ev.sort((a, b) => (b.t || "").localeCompare(a.t || ""));
      html = ev.length ? `<div class="feed">${ev.slice(0, 30).map((e, n) =>
        `<div class="ev" style="--ec:${e.c};animation-delay:${Math.min(n, 12) * 30}ms">${e.h}<time>${ago(e.t)}</time></div>`).join("")}</div>`
        : empty("📡", "No activity yet.");
    }
    list.innerHTML = html;
    d.incidents.forEach(i => delete i._fresh);
  }
  $("#list").addEventListener("click", async e => {
    const rm = e.target.closest("button[data-zone-remove]");
    if (rm) {
      e.stopPropagation();
      if (!confirm("Remove this zone? Tourists will no longer be alerted about it.")) return;
      try {
        await desk("POST", `/api/desk/zones/${rm.dataset.zoneRemove}/remove`);
        notify("🗑️ Zone removed.", "#2ee59d"); state.zoneSig = ""; await refresh(); renderDesk();
      } catch (err) { notify("⚠️ " + esc(err.message), "#ff4d6d"); }
      return;
    }
    const zone = e.target.closest(".item[data-zone]");
    if (zone) { const [a, b] = zone.dataset.zone.split(","); map.flyTo([+a, +b], 12, { duration: 1.1 }); return; }
    const act = e.target.closest("button[data-act]");
    if (act) {  // officer action: acknowledge / resolve (notifies the tourist on Telegram)
      e.stopPropagation();
      act.disabled = true;
      try {
        const r = await fetch(`/api/incident/${act.dataset.id}/${act.dataset.act}`, {
          method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "TravelBuddy", "X-TB-Token": TOKEN },
          body: "{}",
        });
        const body = await r.json();
        if (!body.ok) throw new Error(body.error || "Failed");
        notify(act.dataset.act === "ack" ? `👀 Acknowledged #${act.dataset.id}. The tourist was told help is coming.`
                                         : `✅ Incident #${act.dataset.id} resolved and sealed on the ledger.`, "#2ee59d");
        refresh();
      } catch (err) { notify("⚠️ " + esc(err.message), "#ff4d6d"); act.disabled = false; }
      return;
    }
    const it = e.target.closest(".item"); if (!it) return;
    if (it.dataset.hot) { const [a, b] = it.dataset.hot.split(","); map.flyTo([+a, +b], 15, { duration: 1.1 }); return; }
    select(it.dataset.kind, Number(it.dataset.id));
  });

  // ---------- alerts ----------
  let audio;
  const beep = () => {
    if (!state.sound) return;
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    [0, .22].forEach(off => {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "sine"; o.frequency.value = 880; o.connect(g); g.connect(audio.destination);
      const t = audio.currentTime + off;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + .02); g.gain.exponentialRampToValueAtTime(0.0001, t + .18);
      o.start(t); o.stop(t + .2);
    });
  };
  const notify = (html, color) => {
    const el = document.createElement("div");
    el.className = "toast"; el.style.setProperty("--tc", color);
    el.innerHTML = `<div><span>${html}</span></div>`;
    $("#toasts").prepend(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 5000);
  };
  const toast = i => {
    const el = document.createElement("div");
    el.className = "toast"; el.style.setProperty("--tc", SEV[i.severity]);
    el.innerHTML = `<div><b>${i.severity === "critical" ? "🚨 " : ""}New ${esc(TYPE[i.incident_type] || i.incident_type)} · #${i.incident_id}</b>
      <span>${esc(i.name)} — ${esc((i.description || "").slice(0, 80))}</span></div>`;
    el.onclick = () => { select("inc", i.incident_id); dismiss(); };
    const dismiss = () => { el.classList.add("out"); setTimeout(() => el.remove(), 400); };
    $("#toasts").prepend(el);
    setTimeout(dismiss, i.severity === "critical" ? 12000 : 7000);
  };
  $("#sound").addEventListener("click", e => {
    state.sound = !state.sound;
    e.currentTarget.classList.toggle("on", state.sound);
    e.currentTarget.title = state.sound ? "Alert sound on" : "Alert sound off";
    if (state.sound) beep();
  });
  $("#fit").addEventListener("click", fitAll);
  $("#banner").addEventListener("click", () => {
    const c = state.data?.incidents.find(i => i.status !== "resolved" && i.severity === "critical");
    if (c) select("inc", c.incident_id);
  });

  // ---------- refresh loop ----------
  async function refresh() {
    let d;
    try {
      const r = await fetch("/api/overview?token=" + encodeURIComponent(TOKEN), { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      d = await r.json();
      $("#conn").className = "status-pill"; $("#conn-t").textContent = "Live";
    } catch {
      $("#conn").className = "status-pill off"; $("#conn-t").textContent = "Reconnecting…";
      return;
    }

    // KPIs (animate + bump on change)
    KPIS.forEach(([k]) => {
      const tile = $(`#k-${k}`), v = d.stats[k] ?? 0;
      if (!state.first && state.prevStats[k] !== v) { tile.classList.remove("bump"); void tile.offsetWidth; tile.classList.add("bump"); }
      animateNumber(tile.querySelector("b"), v);
    });
    $("#k-critical").classList.toggle("alarm", d.stats.critical > 0);
    $("#k-pending_checkins").classList.toggle("alarm", d.stats.pending_checkins > 0);
    state.prevStats = { ...d.stats };

    // New incidents → toast + glow + sound
    const fresh = d.incidents.filter(i => !state.knownInc.has(i.incident_id));
    d.incidents.forEach(i => state.knownInc.add(i.incident_id));
    if (!state.first) {
      fresh.forEach(i => { i._fresh = true; toast(i); });
      if (fresh.some(i => i.severity === "critical" || i.severity === "high")) beep();
    }

    // Map
    const open = d.incidents.filter(i => i.status !== "resolved");
    drawAreas(d);
    syncMarkers("tour", d.tourists, t => t.id, t => icon(`tourist${t.checkin_pending ? " pending" : ""}${state.selected === "tour:" + t.id ? " selected" : ""}`, 1), tourPopup);
    syncMarkers("inc", open, i => i.incident_id,
      i => icon(`inc sev-${i.severity}${state.selected === "inc:" + i.incident_id ? " selected" : ""}`, 3), incPopup,
      i => ({ critical: 1000, high: 800, medium: 600, low: 400 })[i.severity]);

    // Banner + vignette
    const crit = open.filter(i => i.severity === "critical").length;
    $("#banner").classList.toggle("show", crit > 0);
    $("#banner-t").textContent = `${crit} critical alert${crit === 1 ? "" : "s"} — click to focus`;
    $("#vignette").classList.toggle("on", crit > 0);

    // Tabs counts + ledger
    $("#c-incidents").textContent = open.length;
    $("#c-tourists").textContent = d.tourists.length;
    $("#c-hotspots").textContent = d.hotspots.filter(h => h.risk_level !== "safe").length;
    const l = d.ledger;
    $("#ledger").innerHTML = `<span class="chain-anim${l.valid ? "" : " bad"}"><i></i><i></i><i></i><i></i></span>
      <span>${l.length} blocks · ${l.valid ? '<b style="color:#2ee59d">intact</b>' : `<b style="color:#ff4d6d">broken at #${l.broken_at}</b>`}</span>
      <code title="${esc(l.head || "")}">${esc(l.head || "")}</code><a href="/ledger" target="_blank" rel="noopener">explore →</a>`;

    state.data = d;
    renderList();
    if (state.first) { state.first = false; setTab(state.tab); setTimeout(fitAll, 300); }
  }

  // ---------- clock ----------
  const tick = () => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
  tick(); setInterval(tick, 1000);

  setTab("incidents");
  refresh();
  setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
