/* Travel Buddy portal — shared micro-interactions */
(() => {
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Reveal-on-scroll with automatic stagger inside [data-stagger] groups
  document.querySelectorAll("[data-stagger]").forEach(group => {
    [...group.children].forEach((el, i) => {
      el.classList.add("reveal");
      el.style.transitionDelay = `${i * 70}ms`;
    });
  });
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.12 });
  document.querySelectorAll(".reveal").forEach(el => io.observe(el));

  // Count-up numbers: <span data-count="42">0</span>
  const countUp = el => {
    const target = Number(el.dataset.count) || 0;
    if (reduced || target === 0) { el.textContent = target.toLocaleString(); return; }
    const start = performance.now(), dur = 1200;
    const tick = now => {
      const p = Math.min(1, (now - start) / dur), eased = 1 - Math.pow(1 - p, 4);
      el.textContent = Math.round(target * eased).toLocaleString();
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  const cio = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) { countUp(e.target); cio.unobserve(e.target); } });
  });
  document.querySelectorAll("[data-count]").forEach(el => cio.observe(el));

  // Verification sequence: checks tick one by one, then the verdict stamps in
  const steps = document.querySelectorAll(".check-step");
  if (steps.length) {
    const verdict = document.querySelector(".verdict");
    steps.forEach((s, i) => setTimeout(() => s.classList.add("done"), reduced ? 0 : 450 + i * 380));
    if (verdict) setTimeout(() => verdict.classList.add("show"), reduced ? 0 : 450 + steps.length * 380 + 150);
  }

  // Holographic tilt on the ID card
  const card = document.querySelector(".id-card");
  if (card && !reduced && matchMedia("(pointer: fine)").matches) {
    card.addEventListener("pointermove", e => {
      const r = card.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
      card.style.transform = `perspective(900px) rotateY(${x * 10}deg) rotateX(${-y * 10}deg)`;
      card.style.setProperty("--mx", `${(x + .5) * 100}%`);
      card.style.setProperty("--my", `${(y + .5) * 100}%`);
    });
    card.addEventListener("pointerleave", () => { card.style.transform = ""; });
  }

  // Ledger blocks: click to expand payload
  document.querySelectorAll(".block[data-toggle]").forEach(b => {
    const toggle = () => b.setAttribute("aria-expanded", String(b.classList.toggle("open")));
    b.addEventListener("click", toggle);
    b.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });

  // Copy-to-clipboard for hashes
  document.querySelectorAll("[data-copy]").forEach(el => {
    el.addEventListener("click", ev => {
      ev.stopPropagation();
      navigator.clipboard?.writeText(el.dataset.copy).then(() => {
        el.classList.add("copied");
        setTimeout(() => el.classList.remove("copied"), 1200);
      });
    });
  });
})();
