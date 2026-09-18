/* Product Builder: photos -> AI draft -> details -> variants -> create. */
(() => {
  const qs = new URLSearchParams(location.search);
  const domainFromToken = (t) => { try { const p = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); return p.iss ? new URL(p.iss).host : ""; } catch { return ""; } };
  const state = {
    domain: qs.get("domain") || (qs.get("saleorApiUrl") ? new URL(qs.get("saleorApiUrl")).host : ""), token: null,
    meta: null, photos: [], selected: [], draft: null, step: 1, variantValues: {}, matrix: [],
  };
  if (qs.get("theme") === "dark" || (!qs.get("theme") && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "dark";

  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    n.append(...children.filter((c) => c !== null && c !== undefined)); return n;
  };
  const notify = (status, text) => window.parent.postMessage({ type: "notification", payload: { actionId: crypto.randomUUID(), status, title: "Product Builder", text } }, "*");
  window.addEventListener("message", (e) => { const d = e.data || {}; if (d.type === "handshake" && d.payload?.token) { const first = !state.token; state.token = d.payload.token; if (!state.domain) state.domain = domainFromToken(state.token); if (first) boot(); } });
  window.parent.postMessage({ type: "notifyReady", payload: { actionId: crypto.randomUUID() } }, "*");

  async function api(path, options = {}) {
    const headers = { "X-Saleor-Domain": state.domain, "X-Saleor-Token": state.token, ...(options.headers || {}) };
    if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) { let d = res.statusText || `HTTP ${res.status}`; try { const j = await res.json(); d = typeof j.detail === "string" ? j.detail : (j.detail?.message + (j.detail?.errors ? " — " + j.detail.errors.map((e) => `${e.field || ""} ${e.message}`).join("; ") : "")); } catch {} throw new Error(d); }
    return res.json();
  }

  // ---- boot / settings -------------------------------------------------------
  async function boot() {
    $("#waiting").hidden = true; $("#app").hidden = false;
    try {
      state.meta = await api("/api/builder/meta");
      fillSettings(); fillMeta(); await loadPhotos(); showStep(1);
    } catch (e) { notify("error", e.message); }
  }
  function fillSettings() {
    const m = state.meta; const d = m.defaults || {};
    const sel = $("#llm-provider"); sel.replaceChildren();
    const names = { openai: "OpenAI", gemini: "Google Gemini", local: "Local GPU (Ollama)" };
    for (const p of ["openai", "gemini", "local"]) sel.append(el("option", { value: p, disabled: !m.llm.available.includes(p) }, names[p] + (m.llm.available.includes(p) ? "" : " (no key)")));
    sel.value = m.llm.provider || m.llm.available[0] || "openai";
    $("#llm-model").value = m.llm.model || "";
    $("#sku-pattern").value = d.sku_pattern || "{brand}-{style}-{color:3}-{size}";
    $("#brand-code").value = d.brand || "ROS";
    $("#reval-url").value = m.storefront.revalidate_url || "";
    $("#reval-secret").placeholder = m.storefront.has_secret ? "•••••• (saved)" : "optional";
    if (!m.llm.available.length) { $("#settings").hidden = false; $("#settings-msg").textContent = "Add an OpenAI or Gemini key in AI Studio → API keys to enable AI drafting."; }
  }
  $("#open-settings").addEventListener("click", () => { $("#settings").hidden = !$("#settings").hidden; });
  $("#close-settings").addEventListener("click", () => { $("#settings").hidden = true; });
  $("#save-settings").addEventListener("click", async () => {
    try {
      await api("/api/builder/settings", { method: "PUT", body: JSON.stringify({
        defaults: { sku_pattern: $("#sku-pattern").value.trim(), brand: $("#brand-code").value.trim(), ptype: $("#ptype").value, category: $("#category").value },
        llm_provider: $("#llm-provider").value, llm_model: $("#llm-model").value.trim(),
        revalidate_url: $("#reval-url").value.trim(), revalidate_secret: $("#reval-secret").value }) });
      $("#reval-secret").value = ""; $("#settings-msg").textContent = "Saved."; notify("success", "Settings saved");
    } catch (e) { notify("error", e.message); }
  });

  function fillMeta() {
    const m = state.meta; const d = m.defaults || {};
    const pt = $("#ptype"); pt.replaceChildren(); for (const p of m.productTypes) pt.append(el("option", { value: p.id }, p.name)); if (d.ptype) pt.value = d.ptype;
    const cat = $("#category"); cat.replaceChildren(el("option", { value: "" }, "— none —")); for (const c of m.categories) cat.append(el("option", { value: c.id }, c.path)); if (d.category) cat.value = d.category;
    const ch = $("#channels"); ch.replaceChildren();
    for (const c of m.channels) ch.append(el("label", {}, el("input", { type: "checkbox", value: c.id, checked: true, "data-name": c.name, "data-cur": c.currencyCode }), `${c.name} (${c.currencyCode})`));
    renderProductAttrs(); renderVariantAttrs();
  }
  $("#ptype").addEventListener("change", () => { renderProductAttrs(); renderVariantAttrs(); });
  const ptype = () => state.meta.productTypes.find((p) => p.id === $("#ptype").value);

  // ---- steps ---------------------------------------------------------------
  const STEPS = ["Photos", "AI draft", "Details", "Variants"];
  function showStep(n) {
    state.step = n;
    document.querySelectorAll(".step").forEach((s) => { s.hidden = Number(s.dataset.step) !== n; });
    $("#result").hidden = true;
    const box = $("#steps"); box.replaceChildren();
    STEPS.forEach((label, i) => box.append(el("span", { class: `chip${i + 1 === n ? " active" : i + 1 < n ? " done" : ""}`, onclick: () => { if (i + 1 < n) showStep(i + 1); } }, `${i + 1} · ${label}`)));
  }

  // ---- step 1: photos --------------------------------------------------------
  async function loadPhotos() {
    const [uploads, generated] = await Promise.all([api("/api/studio/assets?kind=upload"), api("/api/studio/assets?kind=generated")]);
    state.photos = [...uploads, ...generated.filter((g) => g.mime.startsWith("image/"))];
    renderPhotos();
  }
  function renderPhotos() {
    const grid = $("#photos"); grid.replaceChildren();
    for (const a of state.photos) {
      const idx = state.selected.indexOf(a.id);
      grid.append(el("div", { class: `tile${idx >= 0 ? " selected" : ""}`, onclick: () => { idx >= 0 ? state.selected.splice(idx, 1) : state.selected.push(a.id); renderPhotos(); } },
        el("img", { src: a.url, alt: "", loading: "lazy" }), idx >= 0 ? el("span", { class: "check" }, String(idx + 1)) : null,
        el("div", { class: "cap" }, a.kind === "upload" ? (a.label || "upload") : "AI Studio · " + (a.meta?.provider || ""))));
    }
    if (!state.photos.length) grid.append(el("p", { class: "muted" }, "No photos yet — upload some."));
    $("#photos-info").textContent = state.selected.length ? `${state.selected.length} selected · first one is the main image` : "Tick the photos for this product";
    $("#to-draft").disabled = !state.selected.length;
  }
  $("#upload").addEventListener("change", async (e) => {
    for (const file of e.target.files) {
      const fd = new FormData(); fd.append("file", file);
      try { const a = await api("/api/builder/upload", { method: "POST", body: fd }); state.selected.push(a.id); } catch (err) { notify("error", err.message); }
    }
    e.target.value = ""; await loadPhotos();
  });
  $("#to-draft").addEventListener("click", () => showStep(2));

  // ---- step 2: draft ----------------------------------------------------------
  $("#skip-draft").addEventListener("click", () => showStep(3));
  $("#run-draft").addEventListener("click", async () => {
    const btn = $("#run-draft"); btn.disabled = true; $("#draft-msg").textContent = "Asking the AI… (10–30 s)";
    try {
      const r = await api("/api/builder/draft", { method: "POST", body: JSON.stringify({ asset_ids: state.selected, hints: $("#hints").value.trim() }) });
      state.draft = r.draft; applyDraft(r.draft); $("#draft-msg").textContent = `Drafted with ${r.provider} · ${r.model}`; showStep(3);
    } catch (e) { $("#draft-msg").textContent = e.message; notify("error", e.message); }
    finally { btn.disabled = false; }
  });
  function applyDraft(d) {
    const set = (id, v) => { if (v !== undefined && v !== null) $(id).value = Array.isArray(v) ? v.join("\n\n") : v; };
    set("#name-en", d.name); set("#name-de", d.name_de); set("#desc-en", d.description_en); set("#desc-de", d.description_de);
    set("#seo-title-en", d.seo_title_en); set("#seo-desc-en", d.seo_description_en); set("#seo-title-de", d.seo_title_de); set("#seo-desc-de", d.seo_description_de);
    set("#alt-text", d.alt_text_en);
    // category by hint
    if (d.category_hint) { const opt = [...$("#category").options].find((o) => o.textContent.toLowerCase().includes(String(d.category_hint).toLowerCase())); if (opt) $("#category").value = opt.value; }
    // product attributes by name match (colour, material, fit, gender)
    for (const inp of document.querySelectorAll("#product-attrs [data-attr]")) {
      const name = inp.dataset.name.toLowerCase();
      const v = name.includes("colo") ? d.color : name.includes("material") || name.includes("fabric") ? d.material : name.includes("fit") ? d.fit : name.includes("gender") || name.includes("sex") ? d.gender : null;
      if (v) inp.value = v;
    }
    // preselect sizes/colours in the variant step
    for (const attr of ptype().variantAttributes) {
      const name = attr.name.toLowerCase();
      if (name.includes("size") && Array.isArray(d.suggested_sizes)) state.variantValues[attr.id] = d.suggested_sizes.map(String);
      if (name.includes("colo") && d.color) state.variantValues[attr.id] = [d.color];
    }
    renderVariantAttrs();
  }

  // ---- step 3: details --------------------------------------------------------
  function renderProductAttrs() {
    const box = $("#product-attrs"); box.replaceChildren();
    for (const a of (ptype()?.productAttributes || [])) {
      const input = a.values.length ? el("input", { list: `dl-${a.id}`, "data-attr": a.id, "data-name": a.name }) : el("input", { "data-attr": a.id, "data-name": a.name });
      box.append(el("label", {}, a.name + (a.valueRequired ? " *" : ""), input, a.values.length ? el("datalist", { id: `dl-${a.id}` }, ...a.values.map((v) => el("option", { value: v }))) : null));
    }
  }
  $("#to-variants").addEventListener("click", () => {
    if (!$("#name-en").value.trim()) return notify("error", "Product name is required.");
    if (!$("#style-code").value) $("#style-code").value = ($("#name-en").value.split(/\s+/)[0] || "ITEM").replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 6);
    renderVariantAttrs(); renderBasePrices(); showStep(4);
  });

  // ---- step 4: variants --------------------------------------------------------
  function renderVariantAttrs() {
    const box = $("#variant-attrs"); box.replaceChildren();
    for (const a of (ptype()?.variantAttributes || [])) {
      state.variantValues[a.id] = state.variantValues[a.id] || [];
      const chips = el("div", { class: "values" });
      const known = [...new Set([...a.values, ...state.variantValues[a.id]])];
      for (const v of known) chips.append(el("span", { class: `val${state.variantValues[a.id].includes(v) ? " on" : ""}`, onclick: () => { const arr = state.variantValues[a.id]; const i = arr.indexOf(v); i >= 0 ? arr.splice(i, 1) : arr.push(v); renderVariantAttrs(); } }, v));
      const add = el("input", { placeholder: "add value ↵", onkeydown: (e) => { if (e.key === "Enter" && e.target.value.trim()) { state.variantValues[a.id].push(e.target.value.trim()); renderVariantAttrs(); } } });
      chips.append(add);
      box.append(el("label", {}, `${a.name} (${state.variantValues[a.id].length} selected)`, chips));
    }
    if (!(ptype()?.variantAttributes || []).length) box.append(el("p", { class: "muted" }, "This product type has no variant attributes — one variant will be created."));
  }
  function renderBasePrices() {
    const bp = $("#base-prices"); bp.replaceChildren();
    for (const c of [...$("#channels").querySelectorAll("input:checked")]) bp.append(el("label", {}, `${c.dataset.name} ${c.dataset.cur}`, el("input", { type: "number", step: "0.01", min: "0", "data-ch": c.value, value: "49.00" })));
    const bs = $("#base-stocks"); bs.replaceChildren();
    for (const w of state.meta.warehouses) bs.append(el("label", {}, w.name, el("input", { type: "number", min: "0", "data-wh": w.id, value: "10" })));
  }
  $("#build-matrix").addEventListener("click", buildMatrix);
  function skuFor(ctx) {
    const pattern = $("#sku-pattern").value || "{brand}-{style}-{color:3}-{size}";
    return pattern.replace(/\{([a-zA-Z_]+)(?::(\d))?\}/g, (_, key, mod) => { let v = String(ctx[key] || "").replace(/[^A-Za-z0-9]+/g, ""); if (mod) v = v.slice(0, Number(mod)); return v.toUpperCase(); }).replace(/^-+|-+$/g, "").replace(/-{2,}/g, "-");
  }
  function buildMatrix() {
    const attrs = ptype().variantAttributes;
    const lists = attrs.map((a) => (state.variantValues[a.id].length ? state.variantValues[a.id] : [""]));
    const combos = lists.reduce((acc, list) => acc.flatMap((c) => list.map((v) => [...c, v])), [[]]);
    const prices = Object.fromEntries([...$("#base-prices").querySelectorAll("input")].map((i) => [i.dataset.ch, Number(i.value || 0)]));
    const stocks = Object.fromEntries([...$("#base-stocks").querySelectorAll("input")].map((i) => [i.dataset.wh, Number(i.value || 0)]));
    const brand = $("#brand-code").value, style = $("#style-code").value;
    state.matrix = combos.map((combo) => {
      const attributes = Object.fromEntries(attrs.map((a, i) => [a.id, combo[i]]).filter(([, v]) => v));
      const ctx = { brand, style };
      attrs.forEach((a, i) => { const n = a.name.toLowerCase(); ctx[n.includes("colo") ? "color" : n.includes("size") ? "size" : a.slug] = combo[i]; });
      return { attributes, label: combo.filter(Boolean).join(" / ") || "default", sku: skuFor(ctx), prices: { ...prices }, stocks: { ...stocks }, enabled: true, image_asset_id: null };
    });
    renderMatrix();
  }
  function renderMatrix() {
    const t = $("#matrix"); t.replaceChildren();
    const channels = [...$("#channels").querySelectorAll("input:checked")];
    t.append(el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Variant"), el("th", {}, "SKU"), ...channels.map((c) => el("th", {}, `Price ${c.dataset.cur}`)), ...state.meta.warehouses.map((w) => el("th", {}, `Stock ${w.name}`)), el("th", {}, "Image"))));
    const body = el("tbody");
    for (const row of state.matrix) {
      const tr = el("tr", { class: row.enabled ? "" : "off" },
        el("td", {}, el("input", { type: "checkbox", checked: row.enabled, onchange: (e) => { row.enabled = e.target.checked; tr.className = row.enabled ? "" : "off"; } })),
        el("td", {}, row.label),
        el("td", {}, el("input", { value: row.sku, oninput: (e) => { row.sku = e.target.value; } })),
        ...channels.map((c) => el("td", {}, el("input", { class: "num", type: "number", step: "0.01", value: row.prices[c.value], oninput: (e) => { row.prices[c.value] = Number(e.target.value); } }))),
        ...state.meta.warehouses.map((w) => el("td", {}, el("input", { class: "num", type: "number", value: row.stocks[w.id], oninput: (e) => { row.stocks[w.id] = Number(e.target.value); } }))),
        el("td", {}, el("select", { onchange: (e) => { row.image_asset_id = e.target.value || null; } }, el("option", { value: "" }, "—"), ...state.selected.map((id, i) => el("option", { value: id }, `photo ${i + 1}`)))));
      body.append(tr);
    }
    t.append(body);
    $("#matrix-info").textContent = `${state.matrix.length} variant(s)`;
    $("#create").disabled = !state.matrix.length;
  }
  $("#create").addEventListener("click", async () => {
    const btn = $("#create"); btn.disabled = true; $("#create-msg").textContent = "Creating…";
    const attrsFromInputs = Object.fromEntries([...document.querySelectorAll("#product-attrs [data-attr]")].map((i) => [i.dataset.attr, i.value.trim()]).filter(([, v]) => v));
    const body = {
      product_type_id: $("#ptype").value, category_id: $("#category").value || null, name: $("#name-en").value.trim(),
      description_en: $("#desc-en").value.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean),
      seo_title: $("#seo-title-en").value.trim(), seo_description: $("#seo-desc-en").value.trim(),
      product_attributes: attrsFromInputs,
      channels: [...$("#channels").querySelectorAll("input:checked")].map((c) => ({ id: c.value, published: true })),
      variants: state.matrix, image_asset_ids: state.selected, alt_text: $("#alt-text").value.trim(),
      translation_de: $("#name-de").value.trim() ? { name: $("#name-de").value.trim(), description: $("#desc-de").value.split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean), seo_title: $("#seo-title-de").value.trim(), seo_description: $("#seo-desc-de").value.trim() } : null,
    };
    try {
      const r = await api("/api/builder/create", { method: "POST", body: JSON.stringify(body) });
      const list = $("#result-steps"); list.replaceChildren(); for (const s of r.steps) list.append(el("li", {}, el("span", { class: "ok" }, "✓ "), s));
      $("#result-link").href = `${document.referrer ? new URL(document.referrer).origin : ""}/dashboard/products/${encodeURIComponent(r.product.id)}`;
      document.querySelectorAll(".step").forEach((s) => { s.hidden = true; }); $("#result").hidden = false;
      notify("success", `Created "${r.product.name}" with ${r.variants.length} variant(s)`);
    } catch (e) { $("#create-msg").textContent = e.message; notify("error", e.message); btn.disabled = false; }
  });
  $("#restart").addEventListener("click", () => { state.selected = []; state.matrix = []; state.draft = null; document.querySelectorAll("#app input:not([type=checkbox]):not([type=file]), #app textarea").forEach((i) => { if (!["sku-pattern", "brand-code", "reval-url", "llm-model"].includes(i.id)) i.value = ""; }); renderPhotos(); showStep(1); });

  if (qs.get("token")) { state.token = qs.get("token"); if (!state.domain) state.domain = domainFromToken(state.token); boot(); }
})();
