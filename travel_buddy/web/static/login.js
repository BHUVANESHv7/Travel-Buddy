/* Travel Buddy — passwordless sign-in via a code sent to the user's Telegram */
(() => {
  "use strict";
  const $ = s => document.querySelector(s);
  const msg = (text, kind = "") => { const m = $("#msg"); m.textContent = text; m.className = "auth-msg " + kind; };
  let identifier = "";

  const post = async (url, data) => {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "TravelBuddy" },
      body: JSON.stringify(data),
    });
    const body = await r.json().catch(() => ({ ok: false, error: "Unexpected server response." }));
    if (!r.ok || !body.ok) throw new Error(body.error || "Something went wrong.");
    return body;
  };

  const busy = (btn, on, label) => { btn.disabled = on; if (label) btn.textContent = label; };

  $("#step1").addEventListener("submit", async e => {
    e.preventDefault();
    identifier = $("#ident").value.trim();
    const btn = $("#send");
    busy(btn, true, "Sending…");
    try {
      const res = await post("/api/auth/request", { identifier });
      msg(res.message, "ok");
      $("#step1").hidden = true; $("#step2").hidden = false;
      $("#code").value = ""; $("#code").focus();
    } catch (err) {
      msg(err.message, "bad");
    } finally {
      busy(btn, false, "Send me a login code");
    }
  });

  $("#code").addEventListener("input", e => {
    e.target.value = e.target.value.replace(/\D/g, "").slice(0, 6);
    if (e.target.value.length === 6) $("#step2").requestSubmit();
  });

  $("#step2").addEventListener("submit", async e => {
    e.preventDefault();
    const btn = $("#verify");
    busy(btn, true, "Checking…");
    try {
      await post("/api/auth/verify", { identifier, code: $("#code").value });
      msg("Signed in! Opening your portal…", "ok");
      location.href = "/app";
    } catch (err) {
      msg(err.message, "bad");
      $("#code").select();
      document.querySelector(".auth-card").classList.add("shake");
      setTimeout(() => document.querySelector(".auth-card").classList.remove("shake"), 500);
    } finally {
      busy(btn, false, "Sign in");
    }
  });

  $("#resend").addEventListener("click", async () => {
    const btn = $("#resend");
    busy(btn, true, "Sending…");
    try { const res = await post("/api/auth/request", { identifier }); msg("New code sent. " + res.message, "ok"); $("#code").value = ""; $("#code").focus(); }
    catch (err) { msg(err.message, "bad"); }
    finally { busy(btn, false, "↻ Send a new code"); }
  });

  $("#back").addEventListener("click", () => {
    $("#step2").hidden = true; $("#step1").hidden = false; msg(""); $("#ident").focus();
  });
})();
