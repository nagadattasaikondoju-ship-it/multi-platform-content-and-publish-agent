// Shared helpers for every Grow it page.
(function () {
  const header = document.getElementById("siteHeader");
  if (header) {
    const onScroll = () => header.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    const toggle = header.querySelector("[data-menu]");
    if (toggle) toggle.addEventListener("click", () => header.classList.toggle("open"));
  }
})();

const Grow = {
  async api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 204) return null;
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* empty body */
    }
    if (!res.ok) {
      const detail = data && data.detail;
      const message = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Request failed (${res.status})`;
      throw new Error(message);
    }
    return data;
  },

  toast(message, kind) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast show" + (kind === "error" ? " error" : "");
    clearTimeout(Grow._toastTimer);
    Grow._toastTimer = setTimeout(() => (el.className = "toast"), 3200);
  },

  escape(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  },

  debounce(fn, wait) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  },

  formatDate(iso, withTime = true) {
    if (!iso) return "";
    const d = new Date(iso);
    const opts = { day: "numeric", month: "short", year: "numeric" };
    if (withTime) Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleString(undefined, opts);
  },

  badge(p) {
    return `<span class="pbadge" style="background:${p.color}" aria-hidden="true">${Grow.escape(p.short)}</span>`;
  },

  status(s) {
    return `<span class="status s-${s}">${Grow.escape(s)}</span>`;
  },
};

// Render any <time data-local> in the viewer's own timezone.
document.querySelectorAll("time[data-local]").forEach((el) => {
  el.textContent = Grow.formatDate(el.getAttribute("datetime"), el.dataset.local !== "date");
});
