/* AI Studio dashboard page. Loaded in the Saleor dashboard iframe; token arrives via AppBridge handshake. */
(() => {
  const qs = new URLSearchParams(location.search);
  const state = {
    domain: qs.get("domain") || (qs.get("saleorApiUrl") ? new URL(qs.get("saleorApiUrl")).host : ""),
    token: null,
    catalog: null,
    products: [], endCursor: null, search: "",
    product: null,               // selected product
    selectedMedia: new Set(),    // product media ids used as reference
    selectedAssets: new Set(),   // generated asset ids used as reference
    mode: "scene",
    modelPhotos: [], modelPhotoId: null,
    jobs: [], pollTimer: null,
  };
  if (qs.get("theme") === "dark" || (!qs.get("theme") && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "dark";


  // The dashboard doesn't always pass ?domain= to extension pages; the token's issuer is the API URL.
  const domainFromToken = (t) => { try { const p = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); return p.iss ? new URL(p.iss).host : ""; } catch { return ""; } };
  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...children.filter((c) => c !== null && c !== undefined));
    return n;
  };
  const notify = (status, title, text) => window.parent.postMessage({ type: "notification", payload: { actionId: crypto.randomUUID(), status, title, text } }, "*");

  window.addEventListener("message", (e) => {
    const d = e.data || {};
    if (d.type === "handshake" && d.payload?.token) { const first = !state.token; state.token = d.payload.token; if (!state.domain) state.domain = domainFromToken(state.token); if (first) boot(); }
    else if (d.type === "theme" && d.payload) document.documentElement.dataset.theme = d.payload.theme === "dark" ? "dark" : "";
  });
  window.parent.postMessage({ type: "notifyReady", payload: { actionId: crypto.randomUUID() } }, "*");

  async function api(path, options = {}) {
    const headers = { "X-Saleor-Domain": state.domain, "X-Saleor-Token": state.token, ...(options.headers || {}) };
    if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : (j.detail?.message || JSON.stringify(j.detail)); } catch {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  // ---- boot ---------------------------------------------------------------
  async function boot() {
    $("#waiting").hidden = true; $("#app").hidden = false;
    try {
      state.catalog = await api("/api/studio/catalog");
      renderKeys(); renderModeTabs();
      await Promise.all([loadProducts(true), loadModelPhotos()]);
      if (!state.catalog.providers.some((p) => p.configured)) { $("#settings").hidden = false; }
    } catch (e) { notify("error", "AI Studio", e.message); }
  }

  // ---- keys ---------------------------------------------------------------
  function renderKeys() {
    const box = $("#keys"); box.replaceChildren();
    for (const p of state.catalog.providers) {
      const input = el("input", { type: "password", placeholder: p.configured ? "•••••••• (saved)" : p.key_label, autocomplete: "off" });
      const save = el("button", { class: "btn btn-sm", onclick: async () => {
        try { const r = await api("/api/studio/keys", { method: "PUT", body: JSON.stringify({ provider: p.id, api_key: input.value }) });
          p.configured = r.configured; input.value = ""; renderKeys(); renderProviderSelect(); notify("success", "AI Studio", `${p.label} key ${r.configured ? "saved" : "removed"}`);
        } catch (e) { notify("error", "AI Studio", e.message); }
      } }, "Save");
      box.append(el("div", { class: "key-row" },
        el("div", { class: "who" }, el("span", {}, p.label), p.configured ? el("span", { class: "badge badge-ok" }, "configured") : el("span", { class: "badge badge-muted" }, "no key")),
        input, save, el("div", { class: "help" }, p.key_help + (p.configured ? " Leave empty and save to remove the key." : ""))));
    }
  }
  $("#open-settings").addEventListener("click", () => { $("#settings").hidden = !$("#settings").hidden; });
  $("#close-settings").addEventListener("click", () => { $("#settings").hidden = true; });

  // ---- products -----------------------------------------------------------
  async function loadProducts(reset) {
    if (reset) { state.products = []; state.endCursor = null; }
    const params = new URLSearchParams({ search: state.search, first: "20" });
    if (state.endCursor) params.set("after", state.endCursor);
    const page = await api(`/api/studio/products?${params}`);
    state.products.push(...page.items); state.endCursor = page.endCursor;
    $("#load-more").hidden = !page.hasNextPage;
    $("#page-info").textContent = `${state.products.length} of ${page.totalCount}`;
    renderProductList();
  }
  function renderProductList() {
    const ul = $("#product-list"); ul.replaceChildren();
    for (const p of state.products) {
      ul.append(el("li", { class: state.product?.id === p.id ? "active" : "", onclick: () => selectProduct(p) },
        p.thumbnail ? el("img", { src: p.thumbnail, alt: "", loading: "lazy" }) : el("div", { class: "thumb-empty" }),
        el("div", { style: "min-width:0" }, el("div", { class: "name" }, p.name), el("div", { class: "sub" }, `${p.media.length} image${p.media.length === 1 ? "" : "s"}${p.category ? " · " + p.category : ""}`))));
    }
    if (!state.products.length) ul.append(el("li", { class: "muted" }, "No products found."));
  }
  let searchTimer;
  $("#search").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = e.target.value.trim(); loadProducts(true); }, 350); });
  $("#load-more").addEventListener("click", () => loadProducts(false));

  async function selectProduct(p) {
    state.product = p; state.selectedMedia = new Set(p.media.slice(0, 1).map((m) => m.id)); state.selectedAssets = new Set();
    renderProductList();
    $("#empty-state").hidden = true; $("#workspace").hidden = false;
    $("#product-title").textContent = p.name; $("#product-sub").textContent = p.category || "";
    renderProductMedia(); await loadJobs();
  }
  function renderProductMedia() {
    const grid = $("#product-media"); grid.replaceChildren();
    for (const m of state.product.media) {
      const sel = state.selectedMedia.has(m.id);
      grid.append(el("div", { class: `tile${sel ? " selected" : ""}`, onclick: () => { sel ? state.selectedMedia.delete(m.id) : state.selectedMedia.add(m.id); renderProductMedia(); } },
        el("img", { src: m.thumb, alt: m.alt || "" }), sel ? el("span", { class: "check" }, "✓") : null, el("div", { class: "cap" }, m.alt || "product image")));
    }
    // generated images can be re-used as reference (e.g. video from a generated scene)
    for (const a of state.jobs.flatMap((j) => j.assets).filter((a) => a.mime.startsWith("image/"))) {
      const sel = state.selectedAssets.has(a.id);
      grid.append(el("div", { class: `tile${sel ? " selected" : ""}`, onclick: () => { sel ? state.selectedAssets.delete(a.id) : state.selectedAssets.add(a.id); renderProductMedia(); } },
        el("img", { src: a.url, alt: "" }), sel ? el("span", { class: "check" }, "✓") : null, el("div", { class: "cap" }, "generated · " + (a.meta?.provider || ""))));
    }
    if (!grid.children.length) grid.append(el("p", { class: "muted" }, "This product has no images yet — upload one in the product page first."));
  }

  // ---- mode / provider / model -------------------------------------------
  function renderModeTabs() {
    const tabs = $("#mode-tabs"); tabs.replaceChildren();
    for (const m of state.catalog.modes) {
      tabs.append(el("button", { class: `tab${state.mode === m.id ? " active" : ""}`, role: "tab", onclick: () => { state.mode = m.id; renderModeTabs(); } }, m.label));
    }
    $("#mode-help").textContent = state.catalog.modes.find((m) => m.id === state.mode)?.help || "";
    $("#model-photo-block").hidden = state.mode !== "tryon";
    $("#prompt").placeholder = { scene: "e.g. on a wooden table in a bright kitchen, soft morning light", tryon: "optional styling, e.g. tucked in, sleeves rolled, walking in a city street",
      video: "e.g. slow 360° turn on a rotating stand, soft studio light" }[state.mode];
    renderProviderSelect();
  }
  function providersForMode() { return state.catalog.providers.filter((p) => p.models.some((m) => m.modes.includes(state.mode))); }
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
    const p = state.catalog.providers.find((x) => x.id === $("#provider").value);
    const m = p?.models.find((x) => x.id === $("#model").value);
    const box = $("#model-options"); box.replaceChildren();
    for (const [name, values] of Object.entries(m?.options || {})) {
      const s = el("select", { "data-opt": name }); for (const v of values) s.append(el("option", { value: v }, v));
      box.append(el("label", {}, name.replace("_", " "), s));
    }
    $("#cost-hint").textContent = { openai: "Billed by OpenAI per image.", gemini: "Billed by Google per image.", fal: "Billed by fal.ai per image / per second of video." }[p?.id] || "";
  }
  $("#provider").addEventListener("change", renderModelSelect);
  $("#model").addEventListener("change", renderModelOptions);

  // ---- model photos -------------------------------------------------------
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
    if (!state.modelPhotos.length) grid.append(el("p", { class: "muted" }, "No model photos yet. Upload one."));
  }
  $("#model-upload").addEventListener("change", async (e) => {
    const file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append("file", file); fd.append("label", file.name.replace(/\.[^.]+$/, ""));
    try { const a = await api("/api/studio/assets/models", { method: "POST", body: fd }); state.modelPhotoId = a.id; await loadModelPhotos(); }
    catch (err) { notify("error", "AI Studio", err.message); }
    e.target.value = "";
  });

  // ---- generate -----------------------------------------------------------
  $("#generate").addEventListener("click", async () => {
    const p = state.product; if (!p) return;
    const options = { n: Number($("#count").value) };
    for (const s of $("#model-options").querySelectorAll("select")) options[s.dataset.opt] = s.value;
    const body = {
      mode: state.mode, provider: $("#provider").value, model: $("#model").value, product_id: p.id,
      prompt: $("#prompt").value.trim(),
      product_image_urls: p.media.filter((m) => state.selectedMedia.has(m.id)).map((m) => m.url),
      source_asset_ids: [...state.selectedAssets], model_asset_id: state.mode === "tryon" ? state.modelPhotoId : null, options,
    };
    if (state.mode === "scene" && !body.prompt) return notify("error", "AI Studio", "Write a prompt describing the scene.");
    const btn = $("#generate"); btn.disabled = true;
    try { await api("/api/studio/generate", { method: "POST", body: JSON.stringify(body) }); await loadJobs(); }
    catch (e) { notify("error", "AI Studio", e.message); }
    finally { btn.disabled = false; }
  });

  // ---- jobs ---------------------------------------------------------------
  async function loadJobs() {
    if (!state.product) return;
    state.jobs = await api(`/api/studio/jobs?product_id=${encodeURIComponent(state.product.id)}`);
    renderJobs(); renderProductMedia();
    clearTimeout(state.pollTimer);
    if (state.jobs.some((j) => j.status === "queued" || j.status === "running")) state.pollTimer = setTimeout(loadJobs, 4000);
  }
  $("#refresh-jobs").addEventListener("click", loadJobs);
  function renderJobs() {
    const box = $("#jobs"); box.replaceChildren();
    const modeLabel = (id) => state.catalog.modes.find((m) => m.id === id)?.label || id;
    for (const j of state.jobs) {
      const status = j.status === "done" ? el("span", { class: "badge badge-ok" }, "done")
        : j.status === "error" ? el("span", { class: "badge badge-err", title: j.error || "" }, "failed")
        : el("span", {}, el("span", { class: "spinner" }), j.status);
      const grid = el("div", { class: "media-grid" });
      for (const a of j.assets) {
        const isVideo = a.mime.startsWith("video/");
        grid.append(el("div", { class: "tile" },
          isVideo ? el("video", { src: a.url, controls: true, muted: true, loop: true, playsinline: true }) : el("img", { src: a.url, alt: "", onclick: () => openLightbox(a, j) }),
          el("div", { class: "actions" },
            isVideo ? null : el("button", { class: "btn btn-sm btn-primary", onclick: () => attach(a, j) }, "Add to product"),
            el("a", { class: "btn btn-sm", href: a.url, download: `ai-studio-${a.id.slice(0, 8)}.${a.mime.split("/")[1]}` }, "Download"),
            el("button", { class: "btn btn-sm", onclick: async () => { await api(`/api/studio/assets/${a.id}`, { method: "DELETE" }); loadJobs(); } }, "Delete"))));
      }
      box.append(el("div", { class: "job" },
        el("div", { class: "job-head" },
          el("span", {}, el("strong", {}, modeLabel(j.mode)), " · ", j.model, " "),
          el("span", { class: "prompt", title: j.input.prompt || "" }, j.input.prompt || ""),
          status),
        j.status === "error" ? el("p", { class: "muted", style: "padding:10px 14px;margin:0" }, j.error) : grid));
    }
    if (!state.jobs.length) box.append(el("p", { class: "muted" }, "No generations for this product yet."));
  }
  async function attach(a, j) {
    try {
      const r = await api("/api/studio/attach", { method: "POST", body: JSON.stringify({ asset_id: a.id, product_id: state.product.id, alt: (j.input.prompt || "").slice(0, 120) }) });
      notify("success", "AI Studio", "Image added to the product's media.");
      state.product.media.push({ id: r.media.id, url: r.media.url, thumb: r.media.url, alt: "", type: "IMAGE" }); renderProductMedia();
    } catch (e) { notify("error", "AI Studio", e.message); }
  }
  function openLightbox(a, j) {
    $("#lightbox-title").textContent = j.input.prompt || j.model;
    $("#lightbox-body").replaceChildren(el("img", { src: a.url, alt: "" }));
    $("#lightbox").showModal();
  }

  if (qs.get("token")) { state.token = qs.get("token"); if (!state.domain) state.domain = domainFromToken(state.token); boot(); }
})();
