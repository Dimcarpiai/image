/* Image Optimizer dashboard page.
 *
 * The Saleor dashboard loads this page in an iframe with ?domain=&saleorApiUrl=&id=&theme=...
 * and then posts a {type: "handshake", payload: {token}} message (AppBridge protocol).
 * We forward domain + token to our backend as the X-Saleor-* headers the framework expects.
 */
(() => {
  const qs = new URLSearchParams(location.search);
  const state = {
    domain: qs.get("domain") || (qs.get("saleorApiUrl") ? new URL(qs.get("saleorApiUrl")).host : ""),
    token: null,
    settings: null,
    products: [],
    endCursor: null,
    hasNextPage: false,
    search: "",
    busy: new Set(),
  };

  if (qs.get("theme") === "dark" || (!qs.get("theme") && matchMedia("(prefers-color-scheme: dark)").matches)) {
    document.documentElement.dataset.theme = "dark";
  }

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children) node.append(c);
    return node;
  };
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "–";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  };

  // ---- AppBridge ----------------------------------------------------------
  function notifyDashboard(status, title, text) {
    window.parent.postMessage(
      { type: "notification", payload: { actionId: crypto.randomUUID(), status, title, text } },
      "*"
    );
  }

  window.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.type === "handshake" && data.payload && data.payload.token) {
      const first = !state.token;
      state.token = data.payload.token;
      if (first) boot();
    } else if (data.type === "theme" && data.payload) {
      document.documentElement.dataset.theme = data.payload.theme === "dark" ? "dark" : "";
    }
  });
  window.parent.postMessage({ type: "notifyReady", payload: { actionId: crypto.randomUUID() } }, "*");

  // ---- API ----------------------------------------------------------------
  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Saleor-Domain": state.domain,
        "X-Saleor-Token": state.token,
        ...(options.headers || {}),
      },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      throw new Error(detail);
    }
    return options.raw ? res : res.json();
  }

  // ---- Boot ---------------------------------------------------------------
  async function boot() {
    $("#waiting").hidden = true;
    $("#app").hidden = false;
    try {
      const [settings, caps] = await Promise.all([api("/api/settings"), api("/api/capabilities")]);
      state.settings = settings;
      fillSettings(settings);
      if (!caps.avif) $("#avif-option").disabled = true;
      renderStats(caps.stats, caps.avif);
      await loadProducts(true);
    } catch (e) {
      log(`Could not load: ${e.message}`, "err");
    }
  }

  function renderStats(stats, avif) {
    const saved = (stats.original_bytes || 0) - (stats.optimized_bytes || 0);
    $("#stats").textContent = stats.images
      ? `${fmtBytes(saved)} saved across ${stats.images} image${stats.images === 1 ? "" : "s"} so far.` + (avif ? "" : " AVIF output is not available on this server.")
      : `No images optimized yet.` + (avif ? "" : " AVIF output is not available on this server.");
  }

  // ---- Settings -----------------------------------------------------------
  function fillSettings(s) {
    const f = $("#settings-form");
    for (const [k, v] of Object.entries(s)) {
      const input = f.elements[k];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = !!v; else input.value = v;
    }
  }
  function readSettings() {
    const f = $("#settings-form");
    const out = {};
    for (const input of f.elements) {
      if (!input.name) continue;
      out[input.name] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    }
    return out;
  }
  $("#save-settings").addEventListener("click", async () => {
    const btn = $("#save-settings");
    btn.disabled = true;
    try {
      state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(readSettings()) });
      log("Settings saved.", "ok");
      notifyDashboard("success", "Image Optimizer", "Settings saved");
    } catch (e) {
      log(`Settings not saved: ${e.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  });

  // ---- Products -----------------------------------------------------------
  async function loadProducts(reset) {
    const btn = $("#load-more");
    btn.disabled = true;
    if (reset) { state.products = []; state.endCursor = null; renderProducts(); }
    try {
      const params = new URLSearchParams({ search: state.search, first: "20" });
      if (state.endCursor) params.set("after", state.endCursor);
      const page = await api(`/api/products?${params}`);
      state.products.push(...page.items);
      state.endCursor = page.endCursor;
      state.hasNextPage = page.hasNextPage;
      $("#page-info").textContent = `${state.products.length} of ${page.totalCount} products loaded`;
      btn.hidden = !page.hasNextPage;
      renderProducts();
    } catch (e) {
      log(`Could not load products: ${e.message}`, "err");
    } finally {
      btn.disabled = false;
    }
  }
  $("#load-more").addEventListener("click", () => loadProducts(false));
  let searchTimer;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.search = e.target.value.trim(); loadProducts(true); }, 350);
  });

  function productSummary(p) {
    const images = p.media.filter((m) => m.type === "IMAGE");
    const known = images.filter((m) => m.bytes !== null);
    return {
      images,
      total: known.reduce((a, m) => a + m.bytes, 0),
      optimized: images.filter((m) => m.optimized).length,
    };
  }

  function renderProducts() {
    const tbody = $("#product-rows");
    tbody.replaceChildren();
    if (!state.products.length) {
      tbody.append(el("tr", {}, el("td", { colspan: 5, class: "empty" }, state.search ? "No products match this search." : "No products found.")));
      return;
    }
    for (const p of state.products) {
      const { images, total, optimized } = productSummary(p);
      const busy = state.busy.has(p.id);
      const badge = !images.length
        ? el("span", { class: "badge badge-muted" }, "no images")
        : optimized === images.length
        ? el("span", { class: "badge badge-ok" }, "all optimized")
        : el("span", { class: "badge badge-warn" }, `${optimized} of ${images.length}`);

      const row = el("tr", { class: "product", "data-id": p.id },
        el("td", { onclick: () => toggleMedia(p.id) }, el("div", { class: "product-name" }, p.name)),
        el("td", { class: "num" }, String(images.length)),
        el("td", { class: "num" }, fmtBytes(total)),
        el("td", {}, badge),
        el("td", {}, el("div", { class: "product-actions" },
          el("button", { class: "btn btn-sm", onclick: () => toggleMedia(p.id) }, "Images"),
          el("button", { class: "btn btn-sm btn-primary", disabled: busy || !images.length ? "" : null, onclick: () => optimizeProduct(p.id) }, busy ? "Optimizing…" : "Optimize"),
        )),
      );
      tbody.append(row);
      if (p._open) tbody.append(renderMediaRow(p));
    }
  }

  function renderMediaRow(p) {
    const grid = el("div", { class: "media-grid" });
    for (const m of p.media) {
      if (m.type !== "IMAGE") continue;
      grid.append(el("div", { class: "media" },
        el("img", { src: m.thumb, alt: m.alt || "", loading: "lazy" }),
        el("div", { class: "media-meta" },
          el("span", { class: "num" }, fmtBytes(m.bytes)),
          m.optimized ? el("span", { class: "badge badge-ok" }, "optimized") : el("span", { class: "badge badge-muted" }, "original"),
        ),
        el("div", { class: "media-actions" },
          el("button", { class: "btn btn-sm", onclick: () => previewMedia(p, m) }, "Preview"),
          el("button", { class: "btn btn-sm", onclick: () => optimizeProduct(p.id, [m.id], m.optimized) }, m.optimized ? "Redo" : "Optimize"),
        ),
      ));
    }
    if (!grid.children.length) grid.append(el("p", { class: "empty" }, "This product has no images."));
    return el("tr", { class: "media-row" }, el("td", { colspan: 5 }, grid));
  }

  function toggleMedia(id) {
    const p = state.products.find((x) => x.id === id);
    p._open = !p._open;
    renderProducts();
  }

  // ---- Optimize -----------------------------------------------------------
  async function optimizeProduct(productId, mediaIds = null, force = false) {
    if (state.busy.has(productId)) return;
    state.busy.add(productId);
    renderProducts();
    const p = state.products.find((x) => x.id === productId);
    try {
      const result = await api("/api/optimize", {
        method: "POST",
        body: JSON.stringify({ product_id: productId, media_ids: mediaIds, force, settings: readSettings() }),
      });
      let saved = 0, done = 0;
      for (const r of result.results) {
        if (r.status === "optimized") {
          done++; saved += r.original_bytes - r.optimized_bytes;
          log(`${result.product_name}: ${fmtBytes(r.original_bytes)} → ${fmtBytes(r.optimized_bytes)} (${r.saved_percent}% smaller, ${r.format}, ${r.new_size.join("×")})`, "ok");
        } else if (r.status === "error") {
          log(`${result.product_name}: ${r.reason}`, "err");
        } else {
          log(`${result.product_name}: skipped – ${r.reason}`);
        }
      }
      if (done) notifyDashboard("success", "Image Optimizer", `${result.product_name}: ${done} image${done === 1 ? "" : "s"} optimized, ${fmtBytes(saved)} saved`);
      await refreshProduct(productId);
      const caps = await api("/api/capabilities");
      renderStats(caps.stats, caps.avif);
    } catch (e) {
      log(`${p ? p.name : productId}: ${e.message}`, "err");
      notifyDashboard("error", "Image Optimizer", e.message);
    } finally {
      state.busy.delete(productId);
      renderProducts();
    }
  }

  async function refreshProduct(productId) {
    // Cheapest way to refresh one row with the framework's API: re-query by name.
    const p = state.products.find((x) => x.id === productId);
    if (!p) return;
    const page = await api(`/api/products?${new URLSearchParams({ search: p.name, first: "50" })}`);
    const fresh = page.items.find((x) => x.id === productId);
    if (fresh) Object.assign(p, fresh);
  }

  $("#optimize-all").addEventListener("click", async () => {
    const btn = $("#optimize-all");
    btn.disabled = true;
    const targets = state.products.filter((p) => {
      const s = productSummary(p);
      return s.images.length && s.optimized < s.images.length;
    });
    if (!targets.length) {
      log("Nothing to do: every loaded product is already optimized.");
    } else {
      log(`Optimizing ${targets.length} product${targets.length === 1 ? "" : "s"}…`);
      for (const p of targets) await optimizeProduct(p.id);
      log("Finished.", "ok");
    }
    btn.disabled = false;
  });

  // ---- Preview ------------------------------------------------------------
  async function previewMedia(p, m) {
    const dlg = $("#preview");
    $("#preview-title").textContent = p.name;
    $("#preview-before").src = m.url;
    $("#preview-before-cap").textContent = `Original · ${fmtBytes(m.bytes)}`;
    $("#preview-after").removeAttribute("src");
    $("#preview-after-cap").textContent = "Optimizing preview…";
    dlg.showModal();
    try {
      const res = await api("/api/preview", { method: "POST", raw: true, body: JSON.stringify({ url: m.url, settings: readSettings() }) });
      const blob = await res.blob();
      const skipped = res.headers.get("X-Skipped");
      $("#preview-after").src = URL.createObjectURL(blob);
      $("#preview-before-cap").textContent = `Original · ${fmtBytes(Number(res.headers.get("X-Original-Bytes")))} · ${res.headers.get("X-Original-Size")}`;
      $("#preview-after-cap").textContent = skipped
        ? `Would be skipped: ${skipped}`
        : `Optimized · ${fmtBytes(Number(res.headers.get("X-Optimized-Bytes")))} · ${res.headers.get("X-New-Size")} · ${blob.type}`;
    } catch (e) {
      $("#preview-after-cap").textContent = `Preview failed: ${e.message}`;
    }
  }

  // ---- Log ----------------------------------------------------------------
  function log(text, kind = "") {
    const list = $("#log");
    const time = new Date().toLocaleTimeString();
    list.prepend(el("li", {}, el("time", {}, time), el("span", { class: kind }, text)));
    while (list.children.length > 200) list.lastChild.remove();
  }
  $("#clear-log").addEventListener("click", () => $("#log").replaceChildren());

  // Dev convenience: outside the dashboard, allow ?token= for local testing.
  if (qs.get("token")) { state.token = qs.get("token"); boot(); }
})();
