/* AI Studio 0.9 — Studio / Review / Library / Settings. Runs inside the Saleor dashboard iframe. */
(() => {
  const qs = new URLSearchParams(location.search);
  const domainFromToken = (t) => { try { const p = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); return p.iss ? new URL(p.iss).host : ""; } catch { return ""; } };
  const state = {
    domain: qs.get("domain") || (qs.get("saleorApiUrl") ? new URL(qs.get("saleorApiUrl")).host : ""), token: null,
    catalog: null, presets: [], products: [], endCursor: null, search: "", bulk: new Set(),
    product: null, selectedMedia: new Set(), selectedAssets: new Set(), extraRefs: new Map(),
    mode: "scene", modelPhotos: [], modelPhotoId: null, jobs: [], pollTimer: null, review: [], settings: null,
  };
  if (qs.get("theme") === "dark" || (!qs.get("theme") && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "dark";

  // ---- helpers -------------------------------------------------------------
  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...children.filter((c) => c !== null && c !== undefined)); return n;
  };
  const notify = (status, text) => window.parent.postMessage({ type: "notification", payload: { actionId: crypto.randomUUID(), status, title: "AI Studio", text } }, "*");
  function ask({ title, text = "", input = null, check = null, ok = "OK" }) {
    return new Promise((resolve) => {
      const dlg = $("#ask"); $("#ask-title").textContent = title; $("#ask-text").textContent = text; $("#ask-yes").textContent = ok;
      $("#ask-input-wrap").hidden = input === null; $("#ask-input").value = input || "";
      $("#ask-check-wrap").hidden = check === null; $("#ask-check").checked = false; $("#ask-check-label").textContent = check || "";
      const done = (v) => { dlg.close(); resolve(v); };
      $("#ask-yes").onclick = () => done({ ok: true, value: $("#ask-input").value.trim(), checked: $("#ask-check").checked });
      $("#ask-no").onclick = () => done({ ok: false }); dlg.onclose = () => resolve({ ok: false });
      dlg.showModal(); if (input !== null) $("#ask-input").focus();
    });
  }
  const paras = (t) => t.split(/\n\s*\n/).map((x) => x.trim()).filter(Boolean);
  const ownAssetId = (url) => (url.match(/\/media\/([0-9a-f]{32})/) || [])[1];
  const refUrls = () => [...(state.product ? state.product.media.filter((m) => state.selectedMedia.has(m.id)).map((m) => m.url) : []), ...[...state.extraRefs].filter(([, r]) => r.selected !== false).map(([u]) => u)];

  window.addEventListener("message", (e) => { const d = e.data || {}; if (d.type === "handshake" && d.payload?.token) { const first = !state.token; state.token = d.payload.token; if (!state.domain) state.domain = domainFromToken(state.token); if (first) boot(); } else if (d.type === "theme" && d.payload) document.documentElement.dataset.theme = d.payload.theme === "dark" ? "dark" : ""; });
  window.parent.postMessage({ type: "notifyReady", payload: { actionId: crypto.randomUUID() } }, "*");

  async function api(path, options = {}) {
    const headers = { "X-Saleor-Domain": state.domain, "X-Saleor-Token": state.token, ...(options.headers || {}) };
    if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) { let d = res.statusText; try { const j = await res.json(); d = typeof j.detail === "string" ? j.detail : (j.detail?.message + (j.detail?.errors ? " — " + j.detail.errors.map((e) => `${e.field || ""} ${e.message}`).join("; ") : "")); } catch {} throw new Error(d); }
    return res.status === 204 ? null : res.json();
  }

  // ---- views -------------------------------------------------------------------
  function showView(name) {
    document.querySelectorAll(".view").forEach((v) => { v.hidden = v.dataset.view !== name; });
    document.querySelectorAll("#top-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
    if (name === "review") loadReview(); if (name === "library") loadLibrary(); if (name === "settings") loadSettings();
  }
  document.querySelectorAll("#top-tabs .tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

  async function boot() {
    $("#waiting").hidden = true; $("#app").hidden = false;
    try {
      state.catalog = await api("/api/studio/catalog");
      renderKeys(); renderModeTabs();
      await Promise.all([loadProducts(true), loadModelPhotos(), loadPresets(), refreshReviewCount()]);
      if (!state.catalog.providers.some((p) => p.configured)) showView("settings");
    } catch (e) { notify("error", e.message); }
  }

  // ---- products & bulk ------------------------------------------------------
  async function loadProducts(reset) {
    if (reset) { state.products = []; state.endCursor = null; }
    const params = new URLSearchParams({ search: state.search, first: "20" }); if (state.endCursor) params.set("after", state.endCursor);
    const page = await api(`/api/studio/products?${params}`);
    state.products.push(...page.items); state.endCursor = page.endCursor;
    $("#load-more").hidden = !page.hasNextPage; $("#page-info").textContent = `${state.products.length} of ${page.totalCount}`;
    renderProductList();
  }
  function renderProductList() {
    const ul = $("#product-list"); ul.replaceChildren();
    for (const p of state.products) {
      ul.append(el("li", { class: state.product?.id === p.id ? "active" : "" },
        el("input", { type: "checkbox", class: "bulk", checked: state.bulk.has(p.id), onclick: (e) => { e.stopPropagation(); e.target.checked ? state.bulk.add(p.id) : state.bulk.delete(p.id); renderBulkBar(); } }),
        el("div", { class: "row-click", onclick: () => selectProduct(p) },
          p.thumbnail ? el("img", { src: p.thumbnail, alt: "", loading: "lazy" }) : el("div", { class: "thumb-empty" }),
          el("div", { style: "min-width:0" }, el("div", { class: "name" }, p.name), el("div", { class: "sub" }, `${p.media.length} image${p.media.length === 1 ? "" : "s"}${p.category ? " · " + p.category : ""}`)))));
    }
    if (!state.products.length) ul.append(el("li", { class: "muted" }, "No products found."));
    renderBulkBar();
  }
  function renderBulkBar() { $("#bulk-bar").hidden = !state.bulk.size; $("#bulk-count").textContent = `${state.bulk.size} selected`; $("#select-all").checked = state.products.length > 0 && state.products.every((p) => state.bulk.has(p.id)); }
  $("#select-all").addEventListener("change", (e) => { for (const p of state.products) e.target.checked ? state.bulk.add(p.id) : state.bulk.delete(p.id); renderProductList(); });
  $("#bulk-pack").addEventListener("click", async () => {
    const ids = [...state.bulk]; const packs = state.presets.filter((p) => p.in_pack);
    const a = await ask({ title: `Generate pack for ${ids.length} product(s)?`, text: `Runs: ${packs.map((p) => p.name).join(", ") || "no presets marked ★"}. Products that already have an approved on-model image skip the try-on step.`, ok: "Start" });
    if (!a.ok) return;
    try {
      const r = await api("/api/studio/pack-bulk", { method: "POST", body: JSON.stringify({ product_ids: ids, model_asset_id: state.modelPhotoId, skip_if_done: true }) });
      const skipped = r.results.flatMap((x) => x.skipped);
      notify(r.started ? "success" : "error", `${r.started} job(s) started for ${r.results.length} product(s)` + (skipped.length ? `; ${skipped.length} step(s) skipped (${[...new Set(skipped.map((s) => s.reason))].join("; ")})` : ""));
      state.bulk.clear(); renderProductList(); if (state.product) loadJobs();
    } catch (e) { notify("error", e.message); }
  });
  let searchTimer; $("#search").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = e.target.value.trim(); loadProducts(true); }, 350); });
  $("#load-more").addEventListener("click", () => loadProducts(false));

  async function selectProduct(p) {
    state.product = p; state.selectedMedia = new Set(p.media.slice(0, 1).map((m) => m.id)); state.selectedAssets = new Set();
    renderProductList(); $("#empty-state").hidden = true; $("#workspace").hidden = false;
    $("#product-title").textContent = p.name; $("#product-sub").textContent = [p.product_type, p.category, p.description_hint].filter(Boolean).join(" · ");
    smartDefaults(); renderProductMedia(); await loadJobs();
  }
  function smartDefaults() {
    const p = state.product; if (!p) return;
    const prompt = $("#prompt");
    if (!prompt.value.trim() || prompt.dataset.auto === "1") {
      prompt.value = { scene: `${p.description_hint || p.name}, on a clean studio surface, soft daylight, photorealistic e-commerce photo`, tryon: "", video: "slow 360 degree turntable of the product, soft studio light", model: "" }[state.mode] || "";
      prompt.dataset.auto = "1";
    }
    for (const s of $("#model-options").querySelectorAll("select[data-opt=category]")) if (p.tryon_category && [...s.options].some((o) => o.value === p.tryon_category)) s.value = p.tryon_category;
    updateEstimate();
  }
  $("#prompt").addEventListener("input", (e) => { e.target.dataset.auto = "0"; });

  async function refreshProductMedia() {
    const page = await api(`/api/studio/products?${new URLSearchParams({ search: state.product.name, first: "50" })}`);
    const fresh = page.items.find((x) => x.id === state.product.id); if (fresh) { Object.assign(state.product, fresh); renderProductMedia(); }
  }
  function renderProductMedia() {
    const grid = $("#product-media"); grid.replaceChildren();
    for (const m of state.product.media) {
      const sel = state.selectedMedia.has(m.id);
      grid.append(el("div", { class: `tile${sel ? " selected" : ""}`, onclick: () => { sel ? state.selectedMedia.delete(m.id) : state.selectedMedia.add(m.id); renderProductMedia(); } },
        el("img", { src: m.thumb, alt: m.alt || "" }), sel ? el("span", { class: "check" }, "✓") : null,
        el("button", { class: "btn btn-sm del", title: "Remove this image from the product", onclick: async (e) => {
          e.stopPropagation(); const a = await ask({ title: "Remove image?", text: `Remove this image from "${state.product.name}"? This deletes it from Saleor.`, ok: "Remove" }); if (!a.ok) return;
          try { await api("/api/studio/remove-media", { method: "POST", body: JSON.stringify({ product_id: state.product.id, media_id: m.id }) }); state.product.media = state.product.media.filter((x) => x.id !== m.id); state.selectedMedia.delete(m.id); renderProductMedia(); notify("success", "Image removed."); }
          catch (err) { notify("error", err.message); }
        } }, "✕"),
        el("div", { class: "cap" }, m.alt || "product image")));
    }
    for (const a of state.jobs.flatMap((j) => j.assets).filter((a) => a.mime.startsWith("image/") && !a.meta?.mode?.includes("model"))) {
      const sel = state.selectedAssets.has(a.id);
      grid.append(el("div", { class: `tile${sel ? " selected" : ""}`, onclick: () => { sel ? state.selectedAssets.delete(a.id) : state.selectedAssets.add(a.id); renderProductMedia(); } },
        el("img", { src: a.url, alt: "" }), sel ? el("span", { class: "check" }, "✓") : null, el("div", { class: "cap" }, "generated · " + (a.meta?.preset || a.meta?.provider || ""))));
    }
    for (const [url, r] of state.extraRefs) {
      const own = ownAssetId(url); const on = r.selected !== false;
      grid.append(el("div", { class: `tile${on ? " selected" : ""}`, onclick: () => { r.selected = !on; renderProductMedia(); } },
        el("img", { src: r.thumb, alt: "" }), on ? el("span", { class: "check" }, "✓") : null, el("span", { class: "src" }, r.label),
        el("button", { class: "btn btn-sm del", title: "Remove from references", onclick: (e) => { e.stopPropagation(); state.extraRefs.delete(url); renderProductMedia(); } }, "✕"),
        el("div", { class: "actions" }, own ? el("button", { class: "btn btn-sm btn-primary", onclick: async (e) => { e.stopPropagation(); try { await api("/api/studio/attach", { method: "POST", body: JSON.stringify({ asset_id: own, product_id: state.product.id, alt: r.label || "" }) }); state.extraRefs.delete(url); notify("success", `Image added to "${state.product.name}".`); await refreshProductMedia(); } catch (err) { notify("error", err.message); } } }, "Add to product") : el("span", { class: "muted", style: "font-size:12px" }, "URL · reference only"))));
    }
    if (!grid.children.length) grid.append(el("p", { class: "muted" }, "No images yet — upload one, or import from a page."));
  }

  // ---- references: upload / URL / library / other products --------------------
  $("#ref-upload").addEventListener("change", async (e) => { for (const file of e.target.files) { const fd = new FormData(); fd.append("file", file); try { const a = await api("/api/builder/upload", { method: "POST", body: fd }); state.extraRefs.set(location.origin + a.url, { thumb: a.url, label: file.name + (a.meta?.cleaned ? " · cleaned" : ""), selected: true }); } catch (err) { notify("error", err.message); } } e.target.value = ""; renderProductMedia(); });
  const pageSel = new Set();
  $("#ref-url").addEventListener("click", () => { $("#page-picker").showModal(); $("#page-url").focus(); });
  $("#page-url").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#page-go").click(); });
  $("#page-go").addEventListener("click", async () => {
    const url = $("#page-url").value.trim(); if (!url) return;
    const grid = $("#page-grid"); grid.replaceChildren(el("p", { class: "muted" }, "Fetching…")); pageSel.clear(); $("#page-count").textContent = "";
    try {
      const r = await api("/api/studio/page-images", { method: "POST", body: JSON.stringify({ url }) }); grid.replaceChildren();
      for (const u of r.images) {
        const tile = el("div", { class: "tile" }, el("img", { src: u, alt: "", loading: "lazy", referrerpolicy: "no-referrer", onerror: (e) => e.target.closest(".tile").remove() }),
          el("div", { class: "actions" }, el("button", { class: "btn btn-sm btn-primary", onclick: async (e) => { e.stopPropagation(); await importUrls([u], true); } }, "Clone product with this")));
        tile.addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") return; pageSel.has(u) ? pageSel.delete(u) : pageSel.add(u); tile.classList.toggle("selected"); $("#page-count").textContent = `${pageSel.size} selected`; });
        grid.append(tile);
      }
      $("#page-msg").textContent = `${r.images.length} image(s) found.`;
    } catch (e) { grid.replaceChildren(el("p", { class: "muted" }, e.message)); }
  });
  $("#page-import").addEventListener("click", () => importUrls([...pageSel], false));
  async function importUrls(urls, cloneAfter) {
    if (!urls.length) return notify("error", "Tick at least one image.");
    $("#page-msg").textContent = "Importing…";
    try {
      const r = await api("/api/studio/import-urls", { method: "POST", body: JSON.stringify({ urls }) });
      for (const a of r.assets) state.extraRefs.set(location.origin + a.url, { thumb: a.url, label: "imported", selected: true });
      renderProductMedia(); if (r.errors.length) notify("error", r.errors.map((e) => e.error).join("; "));
      $("#page-msg").textContent = `${r.assets.length} imported.`;
      if (cloneAfter && r.assets.length) { $("#page-picker").close(); openClone(r.assets, true); }
    } catch (e) { $("#page-msg").textContent = e.message; }
  }
  $("#open-library").addEventListener("click", () => showView("library"));
  async function loadLibrary() {
    const grid = $("#library-grid"); grid.replaceChildren(el("p", { class: "muted" }, "Loading…"));
    try {
      const items = await api("/api/studio/assets?kind=upload"); grid.replaceChildren();
      for (const a of items) {
        const url = location.origin + a.url; const on = state.extraRefs.has(url);
        grid.append(el("div", { class: `tile${on ? " selected" : ""}`, onclick: (e) => { state.extraRefs.has(url) ? state.extraRefs.delete(url) : state.extraRefs.set(url, { thumb: a.url, label: a.label || "library", selected: true }); e.currentTarget.classList.toggle("selected"); if (state.product) renderProductMedia(); } },
          el("img", { src: a.url, alt: "", loading: "lazy" }), on ? el("span", { class: "check" }, "✓") : null,
          el("button", { class: "btn btn-sm del", onclick: async (e) => { e.stopPropagation(); const c = await ask({ title: "Delete from library?", text: a.label || "", ok: "Delete" }); if (!c.ok) return; await api(`/api/studio/assets/${a.id}`, { method: "DELETE" }); state.extraRefs.delete(url); if (state.product) renderProductMedia(); loadLibrary(); } }, "✕"),
          el("div", { class: "cap", title: a.label || "" }, (a.label || "upload") + (a.meta?.cleaned ? " · cleaned" : ""))));
      }
      if (!items.length) grid.append(el("p", { class: "muted" }, "Library is empty — upload or import pictures."));
    } catch (e) { grid.replaceChildren(el("p", { class: "muted" }, e.message)); }
  }
  $("#library-upload").addEventListener("change", async (e) => { for (const file of e.target.files) { const fd = new FormData(); fd.append("file", file); try { await api("/api/builder/upload", { method: "POST", body: fd }); } catch (err) { notify("error", err.message); } } e.target.value = ""; loadLibrary(); });
  $("#open-picker").addEventListener("click", () => { $("#picker").showModal(); $("#picker-search").value = ""; loadPicker(""); });
  let pickerTimer; $("#picker-search").addEventListener("input", (e) => { clearTimeout(pickerTimer); pickerTimer = setTimeout(() => loadPicker(e.target.value.trim()), 300); });
  async function loadPicker(search) {
    const grid = $("#picker-grid"); grid.replaceChildren(el("p", { class: "muted" }, "Loading…"));
    try {
      const [page, generated] = await Promise.all([api(`/api/studio/products?${new URLSearchParams({ search, first: "30" })}`), search ? Promise.resolve([]) : api("/api/studio/assets?kind=generated")]);
      grid.replaceChildren();
      const add = (url, thumb, label) => { const on = state.extraRefs.has(url); grid.append(el("div", { class: `tile${on ? " selected" : ""}`, onclick: (e) => { state.extraRefs.has(url) ? state.extraRefs.delete(url) : state.extraRefs.set(url, { thumb, label, selected: true }); e.currentTarget.classList.toggle("selected"); renderProductMedia(); } }, el("img", { src: thumb, alt: "", loading: "lazy" }), on ? el("span", { class: "check" }, "✓") : null, el("div", { class: "cap", title: label }, label))); };
      for (const p of page.items) { if (state.product && p.id === state.product.id) continue; for (const m of p.media) add(m.url, m.thumb, p.name); }
      for (const a of generated.filter((g) => g.mime.startsWith("image/") && g.product_id !== state.product?.id)) add(location.origin + a.url, a.url, "generated · " + (a.meta?.preset || a.meta?.provider || ""));
      if (!grid.children.length) grid.append(el("p", { class: "muted" }, "No images found."));
    } catch (e) { grid.replaceChildren(el("p", { class: "muted" }, e.message)); }
  }

  // ---- mode / provider / model / presets / estimate ------------------------
  function renderModeTabs() {
    const tabs = $("#mode-tabs"); tabs.replaceChildren();
    for (const m of state.catalog.modes) tabs.append(el("button", { class: `tab${state.mode === m.id ? " active" : ""}`, role: "tab", onclick: () => { state.mode = m.id; $("#prompt").dataset.auto = "1"; renderModeTabs(); smartDefaults(); } }, m.label));
    $("#mode-help").textContent = state.catalog.modes.find((m) => m.id === state.mode)?.help || "";
    $("#model-photo-block").hidden = state.mode !== "tryon";
    $("#prompt").placeholder = { scene: "describe the scene", tryon: "optional styling, e.g. tucked in, sleeves rolled", video: "camera motion", model: "describe the person: age, hair, build" }[state.mode];
    renderProviderSelect(); renderPresets();
  }
  const providersForMode = () => state.catalog.providers.filter((p) => p.models.some((m) => m.modes.includes(state.mode)));
  function renderProviderSelect() {
    const sel = $("#provider"); const prev = sel.value; sel.replaceChildren();
    for (const p of providersForMode()) sel.append(el("option", { value: p.id }, p.label + (p.configured ? "" : " (no key)")));
    const configured = providersForMode().find((p) => p.configured);
    sel.value = [...sel.options].some((o) => o.value === prev) ? prev : (configured?.id || sel.options[0]?.value);
    renderModelSelect();
  }
  function renderModelSelect() {
    const p = state.catalog.providers.find((x) => x.id === $("#provider").value); const sel = $("#model"); sel.replaceChildren();
    for (const m of (p?.models || []).filter((m) => m.modes.includes(state.mode))) sel.append(el("option", { value: m.id }, m.label));
    renderModelOptions();
  }
  function renderModelOptions() {
    const p = state.catalog.providers.find((x) => x.id === $("#provider").value); const m = p?.models.find((x) => x.id === $("#model").value);
    const box = $("#model-options"); box.replaceChildren();
    for (const [name, values] of Object.entries(m?.options || {})) { const s = el("select", { "data-opt": name, onchange: updateEstimate }); for (const v of values) s.append(el("option", { value: v }, v)); box.append(el("label", {}, name.replace("_", " "), s)); }
    if (state.product) for (const s of box.querySelectorAll("select[data-opt=category]")) if ([...s.options].some((o) => o.value === state.product.tryon_category)) s.value = state.product.tryon_category;
    updateEstimate();
  }
  $("#provider").addEventListener("change", renderModelSelect); $("#model").addEventListener("change", renderModelOptions); $("#count").addEventListener("change", updateEstimate);
  let estTimer;
  function updateEstimate() {
    clearTimeout(estTimer);
    estTimer = setTimeout(async () => {
      try {
        const e = await api("/api/studio/estimate", { method: "POST", body: JSON.stringify({ provider: $("#provider").value, model: $("#model").value, mode: state.mode, n: Number($("#count").value) }) });
        const mins = e.seconds >= 90 ? `~${Math.round(e.seconds / 60)} min` : `~${e.seconds} s`;
        $("#cost-hint").textContent = `≈ €${e.cost_eur.toFixed(2)} · ${mins}` + (e.daily_budget_eur ? ` · today €${e.spent_today_eur.toFixed(2)} / €${e.daily_budget_eur.toFixed(0)}` : "");
      } catch { $("#cost-hint").textContent = ""; }
    }, 200);
  }
  async function loadPresets() { state.presets = await api("/api/studio/presets"); renderPresets(); }
  function renderPresets() { const sel = $("#preset"); sel.replaceChildren(el("option", { value: "" }, "— none —")); for (const p of state.presets.filter((p) => p.mode === state.mode)) sel.append(el("option", { value: p.id }, p.name + (p.in_pack ? " ★" : ""))); }
  $("#preset").addEventListener("change", (e) => {
    const p = state.presets.find((x) => x.id === e.target.value); if (!p) return;
    $("#provider").value = p.provider; renderModelSelect(); $("#model").value = p.model; renderModelOptions();
    for (const s of $("#model-options").querySelectorAll("select")) if (p.options?.[s.dataset.opt]) s.value = p.options[s.dataset.opt];
    if (p.options?.n) $("#count").value = String(p.options.n);
    $("#prompt").value = p.prompt || ""; $("#prompt").dataset.auto = "0"; updateEstimate();
  });
  $("#save-preset").addEventListener("click", async () => {
    const a = await ask({ title: "Save preset", text: "Saves the current mode, provider, model, options and prompt.", input: "", check: "Include in 'Generate pack'", ok: "Save" });
    if (!a.ok || !a.value) return;
    const options = { n: Number($("#count").value) }; for (const s of $("#model-options").querySelectorAll("select")) options[s.dataset.opt] = s.value;
    const preset = { id: a.value.toLowerCase().replace(/[^a-z0-9]+/g, "-"), name: a.value, mode: state.mode, provider: $("#provider").value, model: $("#model").value, prompt: $("#prompt").value.trim(), options, in_pack: a.checked };
    state.presets = [...state.presets.filter((p) => p.id !== preset.id), preset];
    try { await api("/api/studio/presets", { method: "PUT", body: JSON.stringify(state.presets) }); renderPresets(); $("#preset").value = preset.id; notify("success", `Preset "${a.value}" saved`); } catch (e) { notify("error", e.message); }
  });

  // ---- model photos -----------------------------------------------------------
  async function loadModelPhotos() { state.modelPhotos = await api("/api/studio/assets?kind=model"); renderModelPhotos(); }
  function renderModelPhotos() {
    const grid = $("#model-photos"); grid.replaceChildren();
    for (const a of state.modelPhotos) {
      const sel = state.modelPhotoId === a.id;
      grid.append(el("div", { class: `tile${sel ? " selected" : ""}`, onclick: () => { state.modelPhotoId = sel ? null : a.id; renderModelPhotos(); } },
        el("img", { src: a.url, alt: a.label || "" }), sel ? el("span", { class: "check" }, "✓") : null,
        el("button", { class: "btn btn-sm del", onclick: async (e) => { e.stopPropagation(); await api(`/api/studio/assets/${a.id}`, { method: "DELETE" }); if (state.modelPhotoId === a.id) state.modelPhotoId = null; loadModelPhotos(); } }, "✕"),
        el("div", { class: "cap" }, a.label || "model")));
    }
    if (!state.modelPhotos.length) grid.append(el("p", { class: "muted" }, "No model photos yet — upload one or use the “AI model photo” tab."));
  }
  $("#model-upload").addEventListener("change", async (e) => { const file = e.target.files[0]; if (!file) return; const fd = new FormData(); fd.append("file", file); fd.append("label", file.name.replace(/\.[^.]+$/, "")); try { const a = await api("/api/studio/assets/models", { method: "POST", body: fd }); state.modelPhotoId = a.id; await loadModelPhotos(); } catch (err) { notify("error", err.message); } e.target.value = ""; });

  // ---- generate / pack --------------------------------------------------------
  $("#generate").addEventListener("click", async () => {
    const p = state.product; if (!p && state.mode !== "model") return;
    const options = { n: Number($("#count").value) }; for (const s of $("#model-options").querySelectorAll("select")) options[s.dataset.opt] = s.value;
    const body = { mode: state.mode, provider: $("#provider").value, model: $("#model").value, product_id: p ? p.id : null, prompt: $("#prompt").value.trim(),
      product_image_urls: refUrls(), source_asset_ids: [...state.selectedAssets], model_asset_id: state.mode === "tryon" ? state.modelPhotoId : null, options };
    if ((state.mode === "scene" || state.mode === "model") && !body.prompt) return notify("error", "Write a prompt first.");
    const btn = $("#generate"); btn.disabled = true;
    try { await api("/api/studio/generate", { method: "POST", body: JSON.stringify(body) }); await loadJobs(); } catch (e) { notify("error", e.message); } finally { btn.disabled = false; updateEstimate(); }
  });
  $("#generate-pack").addEventListener("click", async () => {
    const p = state.product; if (!p) return;
    const urls = refUrls(); if (!urls.length) return notify("error", "Tick at least one reference image.");
    try {
      const r = await api("/api/studio/pack", { method: "POST", body: JSON.stringify({ product_id: p.id, product_image_urls: urls, model_asset_id: state.modelPhotoId }) });
      notify(r.started.length ? "success" : "error", `${r.started.length} job(s) started (≈ €${r.estimated_cost_eur})` + (r.skipped.length ? `; skipped: ${r.skipped.map((s) => `${s.preset} (${s.reason})`).join(", ")}` : ""));
      await loadJobs();
    } catch (e) { notify("error", e.message); }
  });

  // ---- jobs / results ---------------------------------------------------------
  async function loadJobs() {
    if (!state.product && state.mode !== "model") return;
    const q = state.product ? `?product_id=${encodeURIComponent(state.product.id)}` : "";
    state.jobs = await api(`/api/studio/jobs${q}`);
    if (state.jobs.some((j) => j.mode === "model")) loadModelPhotos();
    renderJobs(); if (state.product) renderProductMedia();
    clearTimeout(state.pollTimer);
    if (state.jobs.some((j) => j.status === "queued" || j.status === "running")) state.pollTimer = setTimeout(loadJobs, 4000); else refreshReviewCount();
  }
  $("#refresh-jobs").addEventListener("click", loadJobs);
  function renderJobs() {
    const box = $("#jobs"); box.replaceChildren();
    const modeLabel = (id) => state.catalog.modes.find((m) => m.id === id)?.label || id;
    for (const j of state.jobs) {
      const status = j.status === "done" ? el("span", { class: "badge badge-ok" }, "done") : j.status === "error" ? el("span", { class: "badge badge-err", title: j.error || "" }, "failed") : el("span", {}, el("span", { class: "spinner" }), j.status);
      const grid = el("div", { class: "media-grid" });
      for (const a of j.assets) {
        const isVideo = a.mime.startsWith("video/"); const approved = a.meta?.status === "approved";
        grid.append(el("div", { class: "tile" },
          isVideo ? el("video", { src: a.url, controls: true, muted: true, loop: true, playsinline: true }) : el("img", { src: a.url, alt: "", onclick: () => openLightbox(a, j) }),
          approved ? el("span", { class: "check", title: "attached" }, "✓") : null,
          el("div", { class: "actions" },
            j.mode === "model" ? el("button", { class: "btn btn-sm btn-primary", onclick: () => { state.modelPhotoId = a.id; state.mode = "tryon"; renderModeTabs(); renderModelPhotos(); } }, "Use for try-on")
              : isVideo ? null : el("button", { class: "btn btn-sm btn-primary", onclick: () => attachTo(a, j, state.product) }, approved ? "Add again" : "Add to product"),
            (j.mode === "model" || isVideo) ? null : el("button", { class: "btn btn-sm", onclick: () => chooseProduct((t) => attachTo(a, j, t)) }, "Other product…"),
            (j.mode === "model" || isVideo) ? null : el("button", { class: "btn btn-sm", onclick: () => openClone([a]) }, "Clone…"),
            el("a", { class: "btn btn-sm", href: a.url, download: `ai-studio-${a.id.slice(0, 8)}.${a.mime.split("/")[1]}` }, "Download"),
            el("button", { class: "btn btn-sm", onclick: async () => { await api(`/api/studio/assets/${a.id}`, { method: "DELETE" }); loadJobs(); } }, "Delete"))));
      }
      box.append(el("div", { class: "job" },
        el("div", { class: "job-head" }, el("span", {}, el("strong", {}, modeLabel(j.mode)), " · ", j.input.preset || j.model), el("span", { class: "prompt", title: j.input.prompt || "" }, j.input.prompt || ""), status),
        j.status === "error" ? el("p", { class: "muted", style: "padding:10px 14px;margin:0" }, j.error) : grid));
    }
    if (!state.jobs.length) box.append(el("p", { class: "muted" }, "No generations for this product yet."));
  }
  async function attachTo(a, j, target) {
    try { await api("/api/studio/attach", { method: "POST", body: JSON.stringify({ asset_id: a.id, product_id: target.id, alt: (j?.input?.prompt || a.label || "").slice(0, 120) }) }); notify("success", `Image added to "${target.name}".`); if (state.product && target.id === state.product.id) await refreshProductMedia(); loadJobs(); }
    catch (e) { notify("error", e.message); }
  }
  function chooseProduct(onPick) {
    const dlg = $("#product-chooser"); dlg.showModal(); $("#chooser-search").value = ""; const list = $("#chooser-list");
    const load = async (search) => { list.replaceChildren(el("li", { class: "muted" }, "Loading…")); const page = await api(`/api/studio/products?${new URLSearchParams({ search, first: "30" })}`); list.replaceChildren();
      for (const p of page.items) list.append(el("li", { onclick: () => { dlg.close(); onPick(p); } }, p.thumbnail ? el("img", { src: p.thumbnail, alt: "" }) : el("div", { class: "thumb-empty" }), el("div", {}, el("div", { class: "name" }, p.name), el("div", { class: "sub" }, `${p.media.length} image(s)`))));
      if (!page.items.length) list.append(el("li", { class: "muted" }, "No products found.")); };
    let t; $("#chooser-search").oninput = (e) => { clearTimeout(t); t = setTimeout(() => load(e.target.value.trim()), 300); }; load("");
  }
  function openLightbox(a, j) { $("#lightbox-title").textContent = j.input.prompt || j.model; $("#lightbox-body").replaceChildren(el("img", { src: a.url, alt: "" })); $("#lightbox").showModal(); }

  // ---- clone --------------------------------------------------------------------
  $("#clone-product").addEventListener("click", () => openClone(state.jobs.flatMap((j) => j.assets).filter((a) => state.selectedAssets.has(a.id) && a.mime.startsWith("image/")), true));
  function openClone(assets, fromHeader = false) {
    if (!state.product) return notify("error", "Pick the source product first.");
    const dlg = $("#clone-dialog"); $("#clone-name").value = state.product.name + " – "; $("#clone-color").value = ""; $("#clone-suffix").value = ""; $("#clone-images").checked = fromHeader;
    $("#clone-msg").textContent = assets.length ? `${assets.length} image(s) will be attached.` : "Tick generated images above to attach them as well.";
    $("#clone-go").onclick = async () => {
      const btn = $("#clone-go"); btn.disabled = true; $("#clone-msg").textContent = "Creating…";
      try { const r = await api("/api/studio/clone", { method: "POST", body: JSON.stringify({ source_product_id: state.product.id, name: $("#clone-name").value.trim(), color: $("#clone-color").value.trim(), sku_suffix: $("#clone-suffix").value.trim(), asset_ids: assets.map((x) => x.id), copy_stock: $("#clone-stock").checked, copy_source_images: $("#clone-images").checked }) });
        dlg.close(); notify("success", `Created "${r.product.name}" — ${r.steps.join(", ")}`); await loadProducts(true); await loadJobs(); }
      catch (e) { $("#clone-msg").textContent = e.message; } finally { btn.disabled = false; }
    };
    dlg.showModal(); $("#clone-name").focus();
  }

  // ---- edit details ---------------------------------------------------------------
  $("#edit-product").addEventListener("click", async () => {
    if (!state.product) return; const dlg = $("#edit-dialog"); $("#ed-msg").textContent = "Loading…"; dlg.showModal();
    try {
      const d = await api(`/api/studio/product-details/${encodeURIComponent(state.product.id)}`);
      $("#edit-title").textContent = `Edit: ${d.name}`; $("#ed-name").value = d.name; $("#ed-slug").value = d.slug || ""; $("#ed-desc").value = d.description.join("\n\n"); $("#ed-seo-title").value = d.seo_title; $("#ed-seo-desc").value = d.seo_description;
      const cat = $("#ed-category"); cat.replaceChildren(el("option", { value: "" }, "— none —")); for (const c of d.categories) cat.append(el("option", { value: c.id }, c.path)); cat.value = d.category_id || "";
      const attrs = $("#ed-attrs"); attrs.replaceChildren(); for (const a of d.attributes) attrs.append(el("label", {}, a.name, el("input", { "data-attr": a.id, value: a.value, list: a.values.length ? `edl-${a.id}` : null }), a.values.length ? el("datalist", { id: `edl-${a.id}` }, ...a.values.map((v) => el("option", { value: v }))) : null));
      $("#ed-name-de").value = d.translation_de.name; $("#ed-desc-de").value = d.translation_de.description.join("\n\n"); $("#ed-seo-title-de").value = d.translation_de.seo_title; $("#ed-seo-desc-de").value = d.translation_de.seo_description;
      const t = $("#ed-variants"); t.replaceChildren(); t.append(el("thead", {}, el("tr", {}, el("th", {}, "Variant"), el("th", {}, "SKU"), ...d.channels.map((c) => el("th", {}, `Price ${c.currencyCode}`)), ...d.warehouses.map((w) => el("th", {}, `Stock ${w.name}`)))));
      const tb = el("tbody"); for (const v of d.variants) tb.append(el("tr", { "data-id": v.id }, el("td", {}, v.label), el("td", {}, el("input", { "data-sku": "", value: v.sku })), ...d.channels.map((c) => el("td", {}, el("input", { class: "num", type: "number", step: "0.01", "data-ch": c.id, value: v.prices[c.id] ?? "" }))), ...d.warehouses.map((w) => el("td", {}, el("input", { class: "num", type: "number", "data-wh": w.id, value: v.stocks[w.id] ?? 0 }))))); t.append(tb); $("#ed-msg").textContent = "";
      $("#ed-save").onclick = async () => {
        const btn = $("#ed-save"); btn.disabled = true; $("#ed-msg").textContent = "Saving…";
        const body = { name: $("#ed-name").value.trim(), slug: $("#ed-slug").value.trim() || null, category_id: $("#ed-category").value || null, description: paras($("#ed-desc").value), seo_title: $("#ed-seo-title").value.trim(), seo_description: $("#ed-seo-desc").value.trim(),
          attributes: Object.fromEntries([...attrs.querySelectorAll("input[data-attr]")].map((i) => [i.dataset.attr, i.value.trim()])),
          translation_de: $("#ed-name-de").value.trim() ? { name: $("#ed-name-de").value.trim(), description: paras($("#ed-desc-de").value), seo_title: $("#ed-seo-title-de").value.trim(), seo_description: $("#ed-seo-desc-de").value.trim() } : null,
          variants: [...tb.querySelectorAll("tr")].map((tr) => ({ id: tr.dataset.id, sku: tr.querySelector("input[data-sku]").value, prices: Object.fromEntries([...tr.querySelectorAll("input[data-ch]")].filter((i) => i.value !== "").map((i) => [i.dataset.ch, Number(i.value)])), stocks: Object.fromEntries([...tr.querySelectorAll("input[data-wh]")].map((i) => [i.dataset.wh, Number(i.value || 0)])) })) };
        try { const r = await api(`/api/studio/product-details/${encodeURIComponent(state.product.id)}`, { method: "PUT", body: JSON.stringify(body) }); state.product.name = r.product.name; $("#product-title").textContent = r.product.name; renderProductList(); dlg.close(); notify("success", r.steps.join(", ")); }
        catch (e) { $("#ed-msg").textContent = e.message; } finally { btn.disabled = false; }
      };
    } catch (e) { $("#ed-msg").textContent = e.message; }
  });

  // ---- review queue + compare view --------------------------------------------
  async function refreshReviewCount() { try { const s = await api("/api/studio/settings"); $("#review-count").textContent = String(s.pending_review); } catch {} }
  async function loadReview() {
    state.review = await api("/api/studio/review"); $("#review-count").textContent = String(state.review.length);
    const grid = $("#review-grid"); grid.replaceChildren();
    for (const a of state.review) grid.append(el("div", { class: "tile" }, el("img", { src: a.url, alt: "", loading: "lazy", onclick: () => openCompare(state.review.indexOf(a)) }),
      el("div", { class: "cap", title: a.label || "" }, (a.product_name || "?") + " · " + (a.meta?.preset || a.meta?.mode || "")),
      el("div", { class: "actions" }, el("button", { class: "btn btn-sm btn-primary", onclick: () => decide(a, "approve") }, "Approve"), el("button", { class: "btn btn-sm", onclick: () => decide(a, "approve_main") }, "Main"),
        el("button", { class: "btn btn-sm", onclick: () => chooseProduct(async (t) => { await attachTo(a, null, t); loadReview(); }) }, "Other…"), el("button", { class: "btn btn-sm", onclick: () => decide(a, "reject") }, "Reject"))));
    if (!state.review.length) grid.append(el("p", { class: "muted" }, "Nothing waiting for review."));
  }
  async function decide(a, decision) {
    try { const r = await api("/api/studio/review", { method: "POST", body: JSON.stringify({ asset_id: a.id, decision }) }); if (decision !== "reject") notify("success", r.main ? "Approved as main image" : "Approved and attached"); }
    catch (e) { notify("error", e.message); }
    state.review = state.review.filter((x) => x.id !== a.id); $("#review-count").textContent = String(state.review.length);
  }
  $("#refresh-review").addEventListener("click", loadReview);
  let cmpIdx = 0;
  function openCompare(i) { if (!state.review.length) return; cmpIdx = Math.max(0, Math.min(i, state.review.length - 1)); renderCompare(); $("#compare").showModal(); }
  function renderCompare() {
    const a = state.review[cmpIdx]; if (!a) { $("#compare").close(); loadReview(); return; }
    $("#cmp-img").src = a.url; $("#compare-title").textContent = a.product_name || ""; $("#cmp-cap").textContent = `${a.meta?.preset || a.meta?.mode || ""} · ${a.meta?.provider || ""} · ${a.label || ""}`; $("#compare-pos").textContent = `${cmpIdx + 1} / ${state.review.length}`;
  }
  async function compareDecide(decision) { const a = state.review[cmpIdx]; if (!a) return; await decide(a, decision); if (cmpIdx >= state.review.length) cmpIdx = state.review.length - 1; renderCompare(); }
  $("#compare-start").addEventListener("click", () => openCompare(0));
  $("#cmp-prev").addEventListener("click", () => { cmpIdx = (cmpIdx - 1 + state.review.length) % state.review.length; renderCompare(); });
  $("#cmp-next").addEventListener("click", () => { cmpIdx = (cmpIdx + 1) % state.review.length; renderCompare(); });
  $("#cmp-approve").addEventListener("click", () => compareDecide("approve")); $("#cmp-main").addEventListener("click", () => compareDecide("approve_main")); $("#cmp-reject").addEventListener("click", () => compareDecide("reject"));
  $("#compare").addEventListener("keydown", (e) => { const k = e.key.toLowerCase(); if (k === "arrowleft") $("#cmp-prev").click(); else if (k === "arrowright") $("#cmp-next").click(); else if (k === "a") compareDecide("approve"); else if (k === "m") compareDecide("approve_main"); else if (k === "r") compareDecide("reject"); else return; e.preventDefault(); });
  $("#compare").addEventListener("close", loadReview);

  // ---- settings ---------------------------------------------------------------------
  function renderKeys() {
    const box = $("#keys"); box.replaceChildren();
    for (const p of state.catalog.providers) {
      const input = el("input", { type: "password", placeholder: p.configured ? "•••••••• (saved)" : p.key_label, autocomplete: "off" });
      box.append(el("div", { class: "key-row" }, el("div", { class: "who" }, el("span", {}, p.label), p.configured ? el("span", { class: "badge badge-ok" }, "configured") : el("span", { class: "badge badge-muted" }, "no key")), input,
        el("button", { class: "btn btn-sm", onclick: async () => { try { const r = await api("/api/studio/keys", { method: "PUT", body: JSON.stringify({ provider: p.id, api_key: input.value }) }); p.configured = r.configured; input.value = ""; renderKeys(); renderProviderSelect(); notify("success", `${p.label} key ${r.configured ? "saved" : "removed"}`); } catch (e) { notify("error", e.message); } } }, "Save"),
        el("div", { class: "help" }, p.key_help + (p.configured ? " Leave empty and save to remove." : ""))));
    }
  }
  async function loadSettings() {
    try {
      const [s, sf] = await Promise.all([api("/api/studio/settings"), api("/api/studio/storefront")]); state.settings = s;
      $("#st-auto").checked = s.auto_pack_new_uploads; $("#st-clean").checked = s.clean_uploads; $("#st-budget").value = s.daily_budget_eur || ""; $("#st-threshold").value = s.review_threshold || ""; $("#st-notify").value = s.notify_url || "";
      $("#st-stats").textContent = `Spent today ≈ €${s.spent_today_eur.toFixed(2)} · ${s.pending_review} image(s) waiting for review.`;
      $("#sf-url").value = sf.revalidate_url; $("#sf-secret").placeholder = sf.has_secret ? "•••••• (saved)" : "optional";
      renderDefaultModels();
    } catch (e) { $("#st-msg").textContent = e.message; }
  }
  $("#st-save").addEventListener("click", async () => {
    try { state.settings = await api("/api/studio/settings", { method: "PUT", body: JSON.stringify({ auto_pack_new_uploads: $("#st-auto").checked, clean_uploads: $("#st-clean").checked, daily_budget_eur: Number($("#st-budget").value || 0), review_threshold: Number($("#st-threshold").value || 0), notify_url: $("#st-notify").value.trim() }) }); $("#st-msg").textContent = "Saved."; updateEstimate(); }
    catch (e) { $("#st-msg").textContent = e.message; }
  });
  function renderDefaultModels() {
    const box = $("#default-models"); box.replaceChildren();
    const dm = state.settings?.default_models || {};
    for (const [type, id] of Object.entries(dm)) { const a = state.modelPhotos.find((m) => m.id === id); box.append(el("div", { class: "dm-row" }, a ? el("img", { src: a.url, alt: "" }) : el("div", { class: "thumb-empty" }), el("span", {}, el("strong", {}, type), " → ", a?.label || id), el("button", { class: "btn btn-sm", onclick: async () => { delete dm[type]; await api("/api/studio/settings", { method: "PUT", body: JSON.stringify({ default_models: dm }) }); loadSettings(); } }, "✕"))); }
    if (!Object.keys(dm).length) box.append(el("p", { class: "muted" }, "No defaults yet."));
    const sel = $("#dm-asset"); sel.replaceChildren(); for (const m of state.modelPhotos) sel.append(el("option", { value: m.id }, m.label || m.id.slice(0, 8)));
  }
  $("#dm-add").addEventListener("click", async () => { const type = $("#dm-type").value.trim() || "*"; const id = $("#dm-asset").value; if (!id) return notify("error", "Upload or generate a model photo first."); const dm = { ...(state.settings?.default_models || {}), [type]: id }; await api("/api/studio/settings", { method: "PUT", body: JSON.stringify({ default_models: dm }) }); $("#dm-type").value = ""; loadSettings(); });
  $("#sf-save").addEventListener("click", async () => { try { const r = await api("/api/studio/storefront", { method: "PUT", body: JSON.stringify({ revalidate_url: $("#sf-url").value.trim(), revalidate_secret: $("#sf-secret").value }) }); $("#sf-secret").value = ""; $("#sf-secret").placeholder = r.has_secret ? "•••••• (saved)" : "optional"; $("#sf-msg").textContent = "Saved."; } catch (e) { $("#sf-msg").textContent = e.message; } });

  if (qs.get("token")) { state.token = qs.get("token"); if (!state.domain) state.domain = domainFromToken(state.token); boot(); }
})();
