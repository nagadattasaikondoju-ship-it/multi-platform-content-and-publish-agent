// Console shell: sidebar, menus, notifications and the ⌘K palette.
(function () {
  const shell = document.getElementById("shell");
  if (!shell) return;

  // --- sidebar: collapsible on desktop, a drawer on mobile ---------------
  const collapse = document.getElementById("collapseNav");
  const setCollapsed = (on) => {
    shell.classList.toggle("collapsed", on);
    if (collapse) collapse.setAttribute("aria-pressed", String(on));
    try { localStorage.setItem("grow.nav.collapsed", on ? "1" : "0"); } catch (_) { /* storage off */ }
  };
  try { if (localStorage.getItem("grow.nav.collapsed") === "1") setCollapsed(true); } catch (_) { /* storage off */ }
  if (collapse) collapse.addEventListener("click", () => setCollapsed(!shell.classList.contains("collapsed")));

  const openNav = document.getElementById("openNav");
  const setDrawer = (open) => {
    shell.classList.toggle("nav-open", open);
    if (openNav) openNav.setAttribute("aria-expanded", String(open));
    if (open) document.querySelector("#sidebar .side-link")?.focus();
  };
  if (openNav) openNav.addEventListener("click", () => setDrawer(true));
  document.querySelectorAll("[data-close-nav]").forEach((el) => el.addEventListener("click", () => setDrawer(false)));

  // --- dropdown menus ----------------------------------------------------
  const menus = [];
  function menu(buttonId, menuId, onOpen) {
    const button = document.getElementById(buttonId);
    const panel = document.getElementById(menuId);
    if (!button || !panel) return;
    const set = (open) => {
      panel.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      if (open && onOpen) onOpen();
    };
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      const opening = panel.hidden;
      menus.forEach((m) => m(false));
      set(opening);
    });
    menus.push(set);
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu")) menus.forEach((m) => m(false));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { menus.forEach((m) => m(false)); setDrawer(false); }
  });

  // --- notifications (computed from your loops) --------------------------
  const bellList = document.getElementById("bellList");
  const bellDot = document.getElementById("bellDot");
  async function loadNotifications() {
    try {
      const items = await Grow.api("GET", "/api/notifications");
      bellDot.hidden = !items.length;
      bellList.innerHTML = items.length
        ? items.map((n) => `<a class="menu-item-rich" href="${Grow.escape(n.url)}"><b class="tone-${Grow.escape(n.tone)}">${Grow.escape(n.title)}</b><span>${Grow.escape(n.body)}</span></a>`).join("")
        : `<div class="menu-empty">You're all caught up.</div>`;
    } catch (_) {
      bellList.innerHTML = `<div class="menu-empty">Couldn't load notifications.</div>`;
    }
  }
  menu("bellBtn", "bellMenu", loadNotifications);
  menu("userBtn", "userMenu");
  loadNotifications();

  // --- command palette ---------------------------------------------------
  const palette = document.getElementById("palette");
  const input = document.getElementById("paletteInput");
  const list = document.getElementById("paletteList");
  let loops = null;
  let results = [];
  let active = 0;

  function draw() {
    const q = input.value.trim().toLowerCase();
    const pages = (window.GROW_NAV || []).map(([label, url]) => ({ label, url, kind: "Page" }));
    const found = (loops || []).map((r) => ({ label: r.title, url: `/app/runs/${r.id}`, kind: "Loop" }));
    results = [...pages, ...found].filter((r) => !q || r.label.toLowerCase().includes(q)).slice(0, 12);
    if (q) results.push({ label: `Create a loop about “${input.value.trim()}”`, url: `/app/new?source=${encodeURIComponent(input.value.trim())}`, kind: "Create" });
    active = Math.min(active, Math.max(results.length - 1, 0));
    list.innerHTML = results.length
      ? results.map((r, i) => `<li role="option" id="pal-${i}" aria-selected="${i === active}" data-i="${i}"><span>${Grow.escape(r.label)}</span><small>${r.kind}</small></li>`).join("")
      : `<li class="menu-empty">No matches</li>`;
    input.setAttribute("aria-activedescendant", results.length ? `pal-${active}` : "");
  }
  async function openPalette() {
    if (!palette.open) palette.showModal();
    input.value = "";
    active = 0;
    draw();
    input.focus();
    if (loops === null) {
      try {
        loops = (await Grow.api("GET", "/api/runs")).map((r) => ({ id: r.id, title: (r.meta && r.meta.title) || r.source.slice(0, 80) }));
      } catch (_) { loops = []; }
      draw();
    }
  }
  const go = (i) => { if (results[i]) location.href = results[i].url; };
  document.getElementById("openPalette").addEventListener("click", openPalette);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  });
  input.addEventListener("input", () => { active = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, results.length - 1); draw(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); go(active); }
  });
  list.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) go(Number(li.dataset.i)); });
  palette.addEventListener("click", (e) => { if (e.target === palette) palette.close(); });
})();
