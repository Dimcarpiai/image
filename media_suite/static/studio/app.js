/* AI Studio 1.0 — catalog production: product → task → references → generate → publish. */
(() => {
  const qs = new URLSearchParams(location.search);
  const domainFromToken = (t) => { try { const p = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); return p.iss ? new URL(p.iss).host : ""; } catch { return ""; } };
  const S = {
    domain: qs.get("domain") || (qs.get("saleorApiUrl") ? new URL(qs.get("saleorApiUrl")).host : ""), token: null,
    catalog: null, tasks: null, products: [], endCursor: null, search: "", bulk: new Set(),
    product: null, task: "product", refs: { product_urls: [], source_asset_id: null, model_asset_id: null, fabric_asset_ids: [], style_url: null, logo_asset_id: null },
    refThumbs: {}, opts: { pose: "standing", background: "white", logo: "preserve", locks: null, angle: "front view", action: "remove_logo", value: "", color: "" },
    variants: null, results: [], pollTimer: null, review: [], looks: [], lockedModel: null, models: [],
  };
  if (qs.get("theme") === "dark" || (!qs.get("theme") && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "dark";

  // ---- helpers ------------------------------------------------------------------
  const $ = (s) => document.querySelector(s);
  const el = (tag, attrs = {}, ...children) => { const n = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) { if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v === true ? "" : v); } n.append(...children.filter((c) => c !== null && c !== undefined)); return n; };
  const notify = (status, text) => window.parent.postMessage({ type: "notification", payload: { actionId: crypto.randomUUID(), status, title: "AI Studio", text } }, "*");
  function ask({ title, text = "", input = null, select = null, ok = "OK" }) {
    return new Promise((resolve) => {
      const dlg = $("#ask"); $("#ask-title").textContent = title; $("#ask-text").textContent = text; $("#ask-yes").textContent = ok;
      $("#ask-input-wrap").hidden = input === null; $("#ask-input").value = input || "";
      $("#ask-select-wrap").hidden = !select; if (select) { const s = $("#ask-select"); s.replaceChildren(); for (const o of select) s.append(el("option", { value: o.value }, o.label)); }
      const done = (v) => { dlg.close(); resolve(v); };
      $("#ask-yes").onclick = () => done({ ok: true, value: $("#ask-input").value.trim(), selected: select ? $("#ask-select").value : null });
      $("#ask-no").onclick = () => done({ ok: false }); dlg.onclose = () => resolve({ ok: false }); dlg.showModal();
    });
  }
  async function api(path, options = {}) {
    const headers = { "X-Saleor-Domain": S.domain, "X-Saleor-Token": S.token, ...(options.headers || {}) };
    if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) { let d = res.statusText || `HTTP ${res.status}`; try { const j = await res.json(); d = typeof j.detail === "string" ? j.detail : (j.detail?.message + (j.detail?.errors ? " — " + j.detail.errors.map((e) => `${e.field || ""} ${e.message}`).join("; ") : "")); } catch {} throw new Error(d); }
    return res.status === 204 ? null : res.json();
  }
  const own = (url) => (String(url).match(/\/media\/([0-9a-f]{32})/) || [])[1];
  window.addEventListener("message", (e) => { const d = e.data || {}; if (d.type === "handshake" && d.payload?.token) { const first = !S.token; S.token = d.payload.token; if (!S.domain) S.domain = domainFromToken(S.token); if (first) boot(); } });
  window.parent.postMessage({ type: "notifyReady", payload: { actionId: crypto.randomUUID() } }, "*");

  function showView(name) { document.querySelectorAll(".view").forEach((v) => { v.hidden = v.dataset.view !== name; }); document.querySelectorAll(".top-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name)); if (name === "review") loadReview(); if (name === "settings") loadSettings(); }
  document.querySelectorAll(".top-tabs .tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

  async function boot() {
    $("#waiting").hidden = true; $("#app").hidden = false;
    try {
      [S.catalog, S.tasks] = await Promise.all([api("/api/studio/catalog"), api("/api/studio/tasks")]);
      S.opts.locks = S.tasks.locks.map((l) => l.id); S.lockedModel = S.tasks.locked_model; S.looks = S.tasks.looks;
      S.models = await api("/api/studio/assets?kind=model");
      renderKeys(); renderAdvanced(); await loadProducts(true); refreshReviewCount();
      if (!S.catalog.providers.some((p) => p.configured)) showView("settings");
    } catch (e) { notify("error", e.message); }
  }

  // ---- 1 · products & bulk ----------------------------------------------------------
  async function loadProducts(reset) {
    if (reset) { S.products = []; S.endCursor = null; }
    const params = new URLSearchParams({ search: S.search, first: "20" }); if (S.endCursor) params.set("after", S.endCursor);
    const page = await api(`/api/studio/products?${params}`); S.products.push(...page.items); S.endCursor = page.endCursor;
    $("#load-more").hidden = !page.hasNextPage; $("#page-info").textContent = `${S.products.length} of ${page.totalCount}`; renderProducts();
  }
  function renderProducts() {
    const ul = $("#product-list"); ul.replaceChildren();
    for (const p of S.products) ul.append(el("li", { class: S.product?.id === p.id ? "active" : "" },
      el("input", { type: "checkbox", class: "bulk", checked: S.bulk.has(p.id), onclick: (e) => { e.stopPropagation(); e.target.checked ? S.bulk.add(p.id) : S.bulk.delete(p.id); renderBulk(); } }),
      el("div", { class: "row-click", onclick: () => selectProduct(p) }, p.thumbnail ? el("img", { src: p.thumbnail, alt: "", loading: "lazy" }) : el("div", { class: "thumb-empty" }),
        el("div", { style: "min-width:0" }, el("div", { class: "name" }, p.name), el("div", { class: "sub" }, `${p.media.length} image${p.media.length === 1 ? "" : "s"}${p.category ? " · " + p.category : ""}`)))));
    if (!S.products.length) ul.append(el("li", { class: "muted" }, "No products found.")); renderBulk();
  }
  function renderBulk() { $("#bulk-bar").hidden = !S.bulk.size; $("#bulk-count").textContent = `${S.bulk.size} selected`; $("#select-all").checked = S.products.length > 0 && S.products.every((p) => S.bulk.has(p.id)); }
  $("#select-all").addEventListener("change", (e) => { for (const p of S.products) e.target.checked ? S.bulk.add(p.id) : S.bulk.delete(p.id); renderProducts(); });
  let st; $("#search").addEventListener("input", (e) => { clearTimeout(st); st = setTimeout(() => { S.search = e.target.value.trim(); loadProducts(true); }, 350); });
  $("#load-more").addEventListener("click", () => loadProducts(false));
  $("#bulk-go").addEventListener("click", async () => {
    const choices = [{ value: "product|white", label: "White-background product photo" }, { value: "model|", label: "On model (locked model / look)" }, { value: "pack|product", label: "Product pack (front, back, side, detail)" }, { value: "pack|model", label: "Model pack" }, { value: "pack|marketing", label: "Marketing pack" }];
    const a = await ask({ title: `Run for ${S.bulk.size} product(s)`, text: "One action for all selected products. Results go to Review.", select: choices, ok: "Run" }); if (!a.ok) return;
    const [task, arg] = a.selected.split("|");
    const look = S.looks.length ? await ask({ title: "Use a look?", select: [{ value: "", label: "No look" }, ...S.looks.map((l) => ({ value: l.id, label: l.name }))], ok: "Continue" }) : { ok: true, selected: "" };
    if (!look.ok) return;
    try {
      const body = { product_ids: [...S.bulk], task: task === "pack" ? "pack" : task, pack: task === "pack" ? arg : "product", options: { ...cleanOpts(), background: task === "product" ? arg : S.opts.background }, look_id: look.selected || null, advanced: advanced() };
      const r = await api("/api/studio/bulk", { method: "POST", body: JSON.stringify(body) });
      const errs = r.results.filter((x) => x.error); notify(r.started ? "success" : "error", `${r.started} job(s) started` + (errs.length ? `; ${errs.length} skipped (${[...new Set(errs.map((x) => x.error))].join("; ")})` : ""));
      S.bulk.clear(); renderProducts(); if (S.product) loadResults();
    } catch (e) { notify("error", e.message); }
  });

  async function selectProduct(p) {
    S.product = p; S.refs.product_urls = p.media.slice(0, 1).map((m) => m.url); S.refs.source_asset_id = null; S.variants = null; S.opts.color = "";
    renderProducts(); $("#empty-state").hidden = true; $("#workspace").hidden = false;
    $("#product-title").textContent = p.name; $("#product-sub").textContent = [p.product_type, p.category, p.description_hint].filter(Boolean).join(" · ");
    renderTasks(); renderRefs(); renderOptions(); await loadResults();
  }

  // ---- 2 · tasks --------------------------------------------------------------------
  function renderTasks() {
    const g = $("#task-grid"); g.replaceChildren();
    for (const t of S.tasks.tasks) g.append(el("button", { class: `task${S.task === t.id ? " active" : ""}`, onclick: () => { S.task = t.id; renderTasks(); renderRefs(); renderOptions(); } }, el("strong", {}, t.label), el("span", {}, t.help)));
    $("#task-help").textContent = S.tasks.tasks.find((t) => t.id === S.task)?.help || "";
  }

  // ---- 3 · references ---------------------------------------------------------------
  const REF_SLOTS = {
    product: [{ key: "product", label: "Product", help: "controls the garment", multi: true }, { key: "fabric", label: "Fabric", help: "optional: true texture", multi: true }, { key: "style", label: "Style", help: "optional: camera, background, crop" }],
    model: [{ key: "product", label: "Product", help: "controls the garment", multi: true }, { key: "model", label: "Model", help: "controls the person" }, { key: "fabric", label: "Fabric", help: "optional" }, { key: "style", label: "Style", help: "optional: copy look from another product" }, { key: "logo", label: "Logo", help: "only if 'Add uploaded logo'" }],
    variants: [{ key: "source", label: "Image to recolour", help: "an approved on-model or product image" }, { key: "product", label: "Product", help: "for accuracy", multi: true }],
    pack: [{ key: "product", label: "Product", help: "controls the garment", multi: true }, { key: "model", label: "Model", help: "for model packs" }, { key: "style", label: "Style", help: "optional" }],
    edit: [{ key: "source", label: "Image to edit", help: "the result you want to change" }, { key: "product", label: "Product", help: "for accuracy", multi: true }, { key: "logo", label: "Logo", help: "only if adding a logo" }],
    video: [{ key: "source", label: "Start image", help: "a product image or a result" }],
  };
  function renderRefs() {
    const box = $("#refs"); box.replaceChildren();
    for (const slot of REF_SLOTS[S.task] || []) {
      const items = slotItems(slot.key);
      const tiles = el("div", { class: "ref-tiles" });
      for (const it of items) tiles.append(el("div", { class: "ref-tile" }, el("img", { src: it.thumb, alt: "" }), el("button", { class: "btn btn-sm del", onclick: () => removeRef(slot.key, it.id) }, "✕")));
      tiles.append(el("button", { class: "ref-add", onclick: () => pickFor(slot.key) }, "+"));
      if (slot.key === "model" && S.lockedModel) tiles.append(el("span", { class: "badge badge-ok", title: "Model lock is on" }, "locked model"));
      box.append(el("div", { class: "ref-slot" }, el("div", { class: "ref-head" }, el("strong", {}, slot.label), el("span", { class: "muted" }, slot.help)), tiles));
    }
  }
  function slotItems(key) {
    const R = S.refs, T = S.refThumbs;
    if (key === "product") return R.product_urls.map((u) => ({ id: u, thumb: T[u] || u }));
    if (key === "source") return R.source_asset_id ? [{ id: R.source_asset_id, thumb: T[R.source_asset_id] }] : [];
    if (key === "model") { const id = R.model_asset_id || S.lockedModel; const m = S.models.find((x) => x.id === id); return m ? [{ id: m.id, thumb: m.url }] : []; }
    if (key === "fabric") return R.fabric_asset_ids.map((id) => ({ id, thumb: T[id] }));
    if (key === "style") return R.style_url ? [{ id: R.style_url, thumb: T[R.style_url] || R.style_url }] : [];
    if (key === "logo") return R.logo_asset_id ? [{ id: R.logo_asset_id, thumb: T[R.logo_asset_id] }] : [];
    return [];
  }
  function removeRef(key, id) {
    const R = S.refs;
    if (key === "product") R.product_urls = R.product_urls.filter((u) => u !== id);
    if (key === "source") R.source_asset_id = null;
    if (key === "model") { R.model_asset_id = null; if (S.lockedModel === id) api("/api/studio/model-lock", { method: "POST", body: JSON.stringify({ asset_id: id, lock: false }) }).then(() => { S.lockedModel = null; renderRefs(); }); }
    if (key === "fabric") R.fabric_asset_ids = R.fabric_asset_ids.filter((x) => x !== id);
    if (key === "style") R.style_url = null;
    if (key === "logo") R.logo_asset_id = null;
    renderRefs();
  }
  function setRef(key, item) {  // item: {id?, url, thumb, kind}
    const R = S.refs; const id = item.id || own(item.url);
    if (key === "product") { if (!R.product_urls.includes(item.url)) R.product_urls.push(item.url); S.refThumbs[item.url] = item.thumb; }
    if (key === "source") { if (!id) return notify("error", "Choose an image stored in the app (a result or an upload)."); R.source_asset_id = id; S.refThumbs[id] = item.thumb; }
    if (key === "model") { if (!id) return notify("error", "Upload the model photo first."); R.model_asset_id = id; if (!S.models.some((m) => m.id === id)) S.models.push({ id, url: item.thumb, label: "model" }); }
    if (key === "fabric") { if (!id) return notify("error", "Upload the fabric photo first."); if (!R.fabric_asset_ids.includes(id)) R.fabric_asset_ids.push(id); S.refThumbs[id] = item.thumb; }
    if (key === "style") { R.style_url = item.url; S.refThumbs[item.url] = item.thumb; if (key === "style") S.opts.background = "custom"; }
    if (key === "logo") { if (!id) return notify("error", "Upload the logo first."); R.logo_asset_id = id; S.refThumbs[id] = item.thumb; S.opts.logo = "add"; }
    renderRefs(); renderOptions();
  }

  // picker: product images, results, uploads, model library, other products, page URL
  async function pickFor(key) {
    const dlg = $("#picker"); $("#picker-title").textContent = { product: "Product reference", source: "Image to use", model: "Model photo", fabric: "Fabric reference", style: "Style from another product", logo: "Company logo" }[key];
    dlg.showModal(); $("#picker-search").value = ""; $("#picker-upload").onchange = async (e) => { for (const f of e.target.files) { const fd = new FormData(); fd.append("file", f); if (key === "model") fd.append("label", f.name); try { const a = await api(key === "model" ? "/api/studio/assets/models" : "/api/builder/upload", { method: "POST", body: fd }); if (key === "model") S.models.push(a); setRef(key, { id: a.id, url: location.origin + a.url, thumb: a.url }); } catch (err) { notify("error", err.message); } } e.target.value = ""; dlg.close(); };
    const grid = $("#picker-grid"); const add = (item, label) => grid.append(el("div", { class: "tile", onclick: () => { setRef(key, item); dlg.close(); } }, el("img", { src: item.thumb, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }), el("div", { class: "cap", title: label }, label)));
    const load = async (q) => {
      grid.replaceChildren(el("p", { class: "muted" }, "Loading…"));
      try {
        if (/^https?:\/\//i.test(q)) {  // page / image URL → fetch candidates, import on pick
          const r = await api("/api/studio/page-images", { method: "POST", body: JSON.stringify({ url: q }) }); grid.replaceChildren();
          for (const u of r.images) grid.append(el("div", { class: "tile", onclick: async () => { const imp = await api("/api/studio/import-urls", { method: "POST", body: JSON.stringify({ urls: [u] }) }); if (imp.assets[0]) { setRef(key, { id: imp.assets[0].id, url: location.origin + imp.assets[0].url, thumb: imp.assets[0].url }); dlg.close(); } else notify("error", imp.errors[0]?.error || "import failed"); } }, el("img", { src: u, alt: "", loading: "lazy", referrerpolicy: "no-referrer", onerror: (e) => e.target.closest(".tile").remove() }), el("div", { class: "cap" }, "from page")));
          return;
        }
        grid.replaceChildren();
        if (key === "model") { for (const m of S.models) add({ id: m.id, url: location.origin + m.url, thumb: m.url }, m.label || "model"); if (!q) grid.append(el("p", { class: "muted", style: "grid-column:1/-1" }, "Upload a photo, or generate one with “On model” → “Keep this model”.")); return; }
        if (!q && S.product) { for (const m of S.product.media) add({ url: m.url, thumb: m.thumb }, S.product.name); for (const r of S.results) for (const a of r.assets) if (a.mime.startsWith("image/")) add({ id: a.id, url: location.origin + a.url, thumb: a.url }, "result · " + (r.label || "")); }
        const [page, uploads] = await Promise.all([api(`/api/studio/products?${new URLSearchParams({ search: q, first: "24" })}`), q ? [] : api("/api/studio/assets?kind=upload")]);
        for (const p of page.items) { if (S.product && p.id === S.product.id) continue; for (const m of p.media) add({ url: m.url, thumb: m.thumb }, p.name); }
        for (const a of uploads) add({ id: a.id, url: location.origin + a.url, thumb: a.url }, a.label || "upload");
        if (!grid.children.length) grid.append(el("p", { class: "muted" }, "Nothing found. Paste a URL or upload."));
      } catch (e) { grid.replaceChildren(el("p", { class: "muted" }, e.message)); }
    };
    let t; $("#picker-search").oninput = (e) => { clearTimeout(t); t = setTimeout(() => load(e.target.value.trim()), 350); }; load("");
  }

  // ---- options per task ---------------------------------------------------------------
  const chip = (label, on, onclick) => el("button", { class: `val${on ? " on" : ""}`, onclick }, label);
  function renderOptions() {
    const box = $("#options"); box.replaceChildren(); const o = S.opts, T = S.tasks;
    const chips = (title, items, get, set) => box.append(el("div", { class: "opt-row" }, el("span", { class: "opt-label" }, title), el("div", { class: "values" }, ...items.map((i) => chip(i.label, get() === i.id, () => { set(i.id); renderOptions(); })))));
    if (S.task === "model" || S.task === "pack") chips("Pose", T.poses, () => o.pose, (v) => (o.pose = v));
    if (["product", "model", "pack"].includes(S.task)) chips("Background", T.backgrounds, () => o.background, (v) => { o.background = v; if (v === "custom" && !S.refs.style_url) pickFor("style"); });
    if (S.task === "product") chips("Angle", [{ id: "front view", label: "Front" }, { id: "back view", label: "Back" }, { id: "side view", label: "Side" }, { id: "fabric detail, macro of the texture and stitching", label: "Fabric detail" }], () => o.angle, (v) => (o.angle = v));
    if (S.task !== "video") chips("Logo", T.logo, () => o.logo, (v) => { o.logo = v; if (v === "add" && !S.refs.logo_asset_id) pickFor("logo"); });
    if (S.task === "edit") { chips("Change", T.edit_actions, () => o.action, (v) => (o.action = v)); const act = T.edit_actions.find((a) => a.id === o.action); if (act?.needs_value) box.append(el("div", { class: "opt-row" }, el("span", { class: "opt-label" }, "To"), el("input", { value: o.value, placeholder: o.action === "change_color" ? "e.g. Navy" : "describe", oninput: (e) => (o.value = e.target.value) }))); }
    if (S.task === "variants") box.append(el("div", { class: "opt-row" }, el("span", { class: "opt-label" }, "Colours"), el("div", { id: "variant-colors", class: "values" }, el("span", { class: "muted" }, "loading…"))));
    if (S.task === "pack") chips("Pack", T.packs, () => o.pack || "product", (v) => (o.pack = v));
    if (S.task !== "video") { box.append(el("div", { class: "opt-row" }, el("span", { class: "opt-label" }, "Product lock"), el("div", { class: "values" }, ...T.locks.map((l) => chip(l.label, o.locks.includes(l.id), () => { o.locks = o.locks.includes(l.id) ? o.locks.filter((x) => x !== l.id) : [...o.locks, l.id]; renderOptions(); })), el("span", { class: "muted", style: "font-size:12px;align-self:center" }, "locked parts must not change")))); }
    if (S.task === "variants") loadVariantColors();
    updateEstimate();
  }
  async function loadVariantColors() {
    try { S.variants = await api(`/api/studio/variants/${encodeURIComponent(S.product.id)}`); const box = $("#variant-colors"); if (!box) return; box.replaceChildren();
      const others = S.variants.colors.filter((c) => c.toLowerCase() !== (S.variants.product_color || "").toLowerCase()); S.opts.selectedColors = S.opts.selectedColors || others;
      for (const c of S.variants.colors) box.append(chip(c + (c === S.variants.product_color ? " (current)" : ""), S.opts.selectedColors.includes(c), () => { S.opts.selectedColors = S.opts.selectedColors.includes(c) ? S.opts.selectedColors.filter((x) => x !== c) : [...S.opts.selectedColors, c]; renderOptions(); }));
      if (!S.variants.colors.length) box.append(el("span", { class: "muted" }, "This product's variants have no colour values — add them in Saleor first."));
    } catch (e) { const box = $("#variant-colors"); if (box) box.replaceChildren(el("span", { class: "muted" }, e.message)); }
  }
  const cleanOpts = () => { const { selectedColors, pack, ...rest } = S.opts; return rest; };
  function renderAdvanced() {
    const ps = $("#adv-provider"); ps.replaceChildren(el("option", { value: "" }, "Automatic")); for (const p of S.catalog.providers.filter((p) => p.configured)) ps.append(el("option", { value: p.id }, p.label));
    ps.onchange = () => { const ms = $("#adv-model"); ms.replaceChildren(el("option", { value: "" }, "Automatic")); const p = S.catalog.providers.find((x) => x.id === ps.value); for (const m of p?.models || []) ms.append(el("option", { value: m.id }, m.label)); updateEstimate(); };
    $("#adv-n").onchange = updateEstimate; $("#adv-model").onchange = updateEstimate;
  }
  const advanced = () => ({ provider: $("#adv-provider").value || null, model: $("#adv-model").value || null, n: Number($("#adv-n").value), size: $("#adv-size").value || null });
  let et; function updateEstimate() { clearTimeout(et); et = setTimeout(async () => { try { const mode = { product: "scene", model: "tryon", variants: "edit", edit: "edit", video: "video", pack: "scene" }[S.task]; const ready = S.tasks.providers_ready[mode]; const prov = $("#adv-provider").value || ready?.[0], model = $("#adv-model").value || ready?.[1]; if (!prov) { $("#cost-hint").textContent = "No provider configured for this task — add a key in Settings."; return; } const n = S.task === "variants" ? (S.opts.selectedColors || []).length || 1 : S.task === "pack" ? 4 : Number($("#adv-n").value); const e = await api("/api/studio/estimate", { method: "POST", body: JSON.stringify({ provider: prov, model, mode, n }) }); $("#cost-hint").textContent = `${prov} · ≈ €${e.cost_eur.toFixed(2)} · ~${e.seconds >= 90 ? Math.round(e.seconds / 60) + " min" : e.seconds + " s"}` + (e.daily_budget_eur ? ` · today €${e.spent_today_eur.toFixed(2)}/${e.daily_budget_eur.toFixed(0)}` : ""); } catch { $("#cost-hint").textContent = ""; } }, 200); }

  // ---- generate ---------------------------------------------------------------------------
  $("#generate").addEventListener("click", async () => {
    if (!S.product) return; const btn = $("#generate"); btn.disabled = true;
    const opts = { ...cleanOpts(), extra: $("#adv-extra").value.trim() };
    try {
      if (S.task === "variants") { if (!S.refs.source_asset_id) throw new Error("Choose the image to recolour (a result or an upload)."); const r = await api("/api/studio/variants/generate", { method: "POST", body: JSON.stringify({ product_id: S.product.id, source_asset_id: S.refs.source_asset_id, colors: S.opts.selectedColors, advanced: advanced() }) }); notify("success", `${r.jobs.length} colour variant(s) started`); }
      else if (S.task === "pack") { const r = await api("/api/studio/pack", { method: "POST", body: JSON.stringify({ product_id: S.product.id, pack: S.opts.pack || "product", refs: S.refs, options: opts, advanced: advanced() }) }); const skipped = r.jobs.filter((j) => j.status === "skipped"); notify(skipped.length < r.jobs.length ? "success" : "error", `${r.jobs.length - skipped.length} of ${r.jobs.length} steps started` + (skipped.length ? ` (${skipped.map((s) => s.error).join("; ")})` : "")); }
      else { const refs = { ...S.refs }; if (S.task === "video" && S.refs.source_asset_id) { refs.product_urls = []; } const r = await api("/api/studio/run", { method: "POST", body: JSON.stringify({ task: S.task, product_id: S.product.id, refs, options: opts, advanced: advanced() }) }); notify("success", `Started with ${r.provider} (≈ €${r.estimated_cost_eur})`); }
      await loadResults();
    } catch (e) { notify("error", e.message); } finally { btn.disabled = false; }
  });

  // ---- results ----------------------------------------------------------------------------
  async function loadResults() {
    if (!S.product) return; S.results = await api(`/api/studio/results?product_id=${encodeURIComponent(S.product.id)}`); renderResults();
    clearTimeout(S.pollTimer); if (S.results.some((r) => ["queued", "running"].includes(r.status))) S.pollTimer = setTimeout(loadResults, 4000); else refreshReviewCount();
  }
  $("#refresh-results").addEventListener("click", loadResults);
  function qcBadge(a) { const q = a.meta?.qc; if (!q) return el("span", { class: "badge badge-muted" }, "checking…"); if (q.status === "ready") return el("span", { class: "badge badge-ok" }, "Ready"); if (q.status === "needs_review") return el("span", { class: "badge badge-warn", title: (q.issues || []).join("\n") }, "Needs review" + (q.issues?.length ? ` · ${q.issues[0]}` : "")); return el("span", { class: "badge badge-muted" }, "unchecked"); }
  function renderResults() {
    const box = $("#results"); box.replaceChildren();
    for (const r of S.results) {
      const status = r.status === "done" ? null : r.status === "error" ? el("span", { class: "badge badge-err", title: r.error || "" }, "failed") : el("span", {}, el("span", { class: "spinner" }), r.status);
      const grid = el("div", { class: "media-grid" });
      for (const a of r.assets) {
        const isVideo = a.mime.startsWith("video/"); const pub = a.meta?.published_as;
        grid.append(el("div", { class: "tile" },
          isVideo ? el("video", { src: a.url, controls: true, muted: true, loop: true, playsinline: true }) : el("img", { src: a.url, alt: "", onclick: () => { $("#lightbox-title").textContent = r.label; $("#lightbox-body").replaceChildren(el("img", { src: a.url, alt: "" })); $("#lightbox").showModal(); } }),
          pub ? el("span", { class: "check", title: `published: ${pub}` }, "✓") : null,
          el("div", { class: "cap" }, isVideo ? "video" : qcBadge(a), el("span", { class: "muted" }, ` V${a.meta?.version || 1}`)),
          el("div", { class: "actions" }, actionMenu(a, r))));
      }
      box.append(el("div", { class: "job" }, el("div", { class: "job-head" }, el("span", {}, el("strong", {}, r.label)), el("span", { class: "prompt" }, r.task || ""), status), r.status === "error" ? el("p", { class: "muted", style: "padding:10px 14px;margin:0" }, r.error) : grid));
    }
    if (!S.results.length) box.append(el("p", { class: "muted" }, "No results yet."));
  }
  function actionMenu(a, r) {
    const isVideo = a.mime.startsWith("video/");
    const sel = el("select", { class: "action-select", onchange: async (e) => { const v = e.target.value; e.target.value = ""; if (v) await doAction(v, a, r); } },
      el("option", { value: "" }, "Actions…"),
      ...(isVideo ? [] : [el("option", { value: "add" }, "Add to product"), el("option", { value: "add_variant" }, "Add to variant…"), el("option", { value: "thumbnail" }, "Set as thumbnail"), el("option", { value: "approve" }, "Approve (keep)")]),
      ...(!isVideo && (r.task === "model" || a.meta?.mode === "tryon") ? [el("option", { value: "keep_model" }, "Keep this model")] : []),
      ...(!isVideo ? [el("option", { value: "use_source" }, "Use as source for edit/variants"), el("option", { value: "variants" }, "Make colour variants"), el("option", { value: "versions" }, "Versions"), el("option", { value: "other" }, "Add to another product…")] : []),
      el("option", { value: "download" }, "Download"), el("option", { value: "delete" }, "Delete"));
    return sel;
  }
  async function doAction(v, a, r) {
    try {
      if (["add", "thumbnail", "approve", "delete"].includes(v)) { const res = await api("/api/studio/publish", { method: "POST", body: JSON.stringify({ asset_id: a.id, action: v }) }); notify("success", { add: "Added to product", thumbnail: "Set as thumbnail", approve: "Approved", delete: "Deleted" }[v]); if (v === "delete") { S.results = S.results.map((x) => ({ ...x, assets: x.assets.filter((y) => y.id !== a.id) })); renderResults(); } else loadResults(); return; }
      if (v === "add_variant") { const vi = S.variants || await api(`/api/studio/variants/${encodeURIComponent(S.product.id)}`); const pick = await ask({ title: "Add to which variant?", select: vi.variants.map((x) => ({ value: x.id, label: `${x.label} ${x.sku ? "· " + x.sku : ""}${a.meta?.variant_id === x.id ? " (suggested)" : ""}` })), ok: "Add" }); if (!pick.ok) return; await api("/api/studio/publish", { method: "POST", body: JSON.stringify({ asset_id: a.id, action: "add_variant", variant_id: pick.selected }) }); notify("success", "Added to the variant"); loadResults(); return; }
      if (v === "keep_model") { const lk = await api("/api/studio/model-lock", { method: "POST", body: JSON.stringify({ asset_id: a.id, label: `${S.product.name} model` }) }); S.lockedModel = lk.locked_model; S.models = await api("/api/studio/assets?kind=model"); notify("success", "Model locked — every future “On model” uses this person"); renderRefs(); return; }
      if (v === "use_source") { S.refs.source_asset_id = a.id; S.refThumbs[a.id] = a.url; notify("success", "Set as source image"); renderRefs(); return; }
      if (v === "variants") { S.task = "variants"; S.refs.source_asset_id = a.id; S.refThumbs[a.id] = a.url; renderTasks(); renderRefs(); renderOptions(); window.scrollTo({ top: $("#refs-panel").offsetTop - 20, behavior: "smooth" }); return; }
      if (v === "versions") { const chain = await api(`/api/studio/versions/${a.id}`); const g = $("#versions-grid"); g.replaceChildren(); for (const x of chain) g.append(el("div", { class: `tile${x.current ? " selected" : ""}` }, el("img", { src: x.url, alt: "" }), el("div", { class: "cap" }, `V${x.meta?.version || 1} · ${x.meta?.task || x.kind}`), el("div", { class: "actions" }, el("button", { class: "btn btn-sm", onclick: () => { S.refs.source_asset_id = x.id; S.refThumbs[x.id] = x.url; $("#versions").close(); renderRefs(); } }, "Use as source"), el("button", { class: "btn btn-sm btn-primary", onclick: async () => { await api("/api/studio/publish", { method: "POST", body: JSON.stringify({ asset_id: x.id, action: "add" }) }); notify("success", "Added to product"); } }, "Add")))); $("#versions").showModal(); return; }
      if (v === "other") { const q = await ask({ title: "Add to which product?", input: "", ok: "Search" }); if (!q.ok) return; const page = await api(`/api/studio/products?${new URLSearchParams({ search: q.value, first: "20" })}`); const pick = await ask({ title: "Choose product", select: page.items.map((p) => ({ value: p.id, label: p.name })), ok: "Add" }); if (!pick.ok) return; await api("/api/studio/publish", { method: "POST", body: JSON.stringify({ asset_id: a.id, action: "add", product_id: pick.selected }) }); notify("success", "Added"); return; }
      if (v === "download") { const l = document.createElement("a"); l.href = a.url; l.download = `ai-${a.id.slice(0, 8)}.${a.mime.split("/")[1]}`; l.click(); }
    } catch (e) { notify("error", e.message); }
  }

  // ---- review + compare ------------------------------------------------------------------
  async function refreshReviewCount() { try { const s = await api("/api/studio/settings"); $("#review-count").textContent = String(s.pending_review); } catch {} }
  async function loadReview() {
    S.review = await api("/api/studio/review"); $("#review-count").textContent = String(S.review.length);
    const g = $("#review-grid"); g.replaceChildren();
    for (const a of S.review) g.append(el("div", { class: "tile" }, el("img", { src: a.url, alt: "", loading: "lazy", onclick: () => openCompare(S.review.indexOf(a)) }), el("div", { class: "cap" }, qcBadge(a), el("span", { class: "muted" }, ` ${a.product_name || ""}`)),
      el("div", { class: "actions" }, el("button", { class: "btn btn-sm btn-primary", onclick: () => decide(a, "add") }, "Add"), el("button", { class: "btn btn-sm", onclick: () => decide(a, "thumbnail") }, "Thumbnail"), el("button", { class: "btn btn-sm", onclick: () => decide(a, "delete") }, "Delete"))));
    if (!S.review.length) g.append(el("p", { class: "muted" }, "Nothing waiting for review."));
  }
  async function decide(a, action) { try { await api("/api/studio/publish", { method: "POST", body: JSON.stringify({ asset_id: a.id, action, product_id: a.product_id }) }); } catch (e) { notify("error", e.message); } S.review = S.review.filter((x) => x.id !== a.id); $("#review-count").textContent = String(S.review.length); }
  $("#refresh-review").addEventListener("click", loadReview);
  let ci = 0; function openCompare(i) { if (!S.review.length) return; ci = Math.min(i, S.review.length - 1); renderCompare(); $("#compare").showModal(); }
  function renderCompare() { const a = S.review[ci]; if (!a) { $("#compare").close(); loadReview(); return; } $("#cmp-img").src = a.url; $("#compare-title").textContent = a.product_name || ""; $("#cmp-cap").replaceChildren(qcBadge(a), el("span", { class: "muted" }, ` ${a.label || ""}`)); $("#compare-pos").textContent = `${ci + 1} / ${S.review.length}`; }
  async function cd(action) { const a = S.review[ci]; if (!a) return; await decide(a, action); if (ci >= S.review.length) ci = S.review.length - 1; renderCompare(); }
  $("#compare-start").addEventListener("click", () => openCompare(0)); $("#cmp-prev").addEventListener("click", () => { ci = (ci - 1 + S.review.length) % S.review.length; renderCompare(); }); $("#cmp-next").addEventListener("click", () => { ci = (ci + 1) % S.review.length; renderCompare(); });
  $("#cmp-approve").addEventListener("click", () => cd("add")); $("#cmp-main").addEventListener("click", () => cd("thumbnail")); $("#cmp-reject").addEventListener("click", () => cd("delete"));
  $("#compare").addEventListener("keydown", (e) => { const k = e.key.toLowerCase(); if (k === "arrowleft") $("#cmp-prev").click(); else if (k === "arrowright") $("#cmp-next").click(); else if (k === "a") cd("add"); else if (k === "t") cd("thumbnail"); else if (k === "r") cd("delete"); else return; e.preventDefault(); });
  $("#compare").addEventListener("close", loadReview);

  // ---- settings -------------------------------------------------------------------------------
  function renderKeys() { const box = $("#keys"); box.replaceChildren(); for (const p of S.catalog.providers) { const input = el("input", { type: "password", placeholder: p.configured ? "•••••••• (saved)" : p.key_label, autocomplete: "off" }); box.append(el("div", { class: "key-row" }, el("div", { class: "who" }, el("span", {}, p.label), p.configured ? el("span", { class: "badge badge-ok" }, "configured") : el("span", { class: "badge badge-muted" }, "no key")), input, el("button", { class: "btn btn-sm", onclick: async () => { try { const r = await api("/api/studio/keys", { method: "PUT", body: JSON.stringify({ provider: p.id, api_key: input.value }) }); p.configured = r.configured; input.value = ""; S.tasks = await api("/api/studio/tasks"); renderKeys(); renderAdvanced(); notify("success", `${p.label} key ${r.configured ? "saved" : "removed"}`); } catch (e) { notify("error", e.message); } } }, "Save"), el("div", { class: "help" }, p.key_help))); } }
  async function loadSettings() {
    try {
      const [s, sf, t] = await Promise.all([api("/api/studio/settings"), api("/api/studio/storefront"), api("/api/studio/tasks")]); S.looks = t.looks; S.lockedModel = t.locked_model; S.models = await api("/api/studio/assets?kind=model");
      $("#st-auto").checked = s.auto_pack_new_uploads; $("#st-clean").checked = s.clean_uploads; $("#st-budget").value = s.daily_budget_eur || ""; $("#st-notify").value = s.notify_url || ""; $("#st-stats").textContent = `Spent today ≈ €${s.spent_today_eur.toFixed(2)} · ${s.pending_review} waiting for review.`;
      $("#sf-url").value = sf.revalidate_url; $("#sf-secret").placeholder = sf.has_secret ? "•••••• (saved)" : "optional";
      const lm = S.models.find((m) => m.id === S.lockedModel); const box = $("#locked-model-box"); box.replaceChildren(el("h3", {}, "Locked model"), lm ? el("div", { class: "dm-row" }, el("img", { src: lm.url, alt: "" }), el("span", {}, lm.label || "model"), el("button", { class: "btn btn-sm", onclick: async () => { await api("/api/studio/model-lock", { method: "POST", body: JSON.stringify({ asset_id: lm.id, lock: false }) }); loadSettings(); } }, "Unlock")) : el("p", { class: "muted" }, "None. On any “On model” result choose Actions → Keep this model."));
      const bg = $("#look-bg"); bg.replaceChildren(); for (const b of t.backgrounds) bg.append(el("option", { value: b.id }, b.label)); const po = $("#look-pose"); po.replaceChildren(); for (const p of t.poses) po.append(el("option", { value: p.id }, p.label));
      const ll = $("#looks-list"); ll.replaceChildren(); for (const l of S.looks) ll.append(el("div", { class: "dm-row" }, el("span", {}, el("strong", {}, l.name), ` · ${l.background} · ${l.pose}${l.model_asset_id ? " · model" : ""}`), el("button", { class: "btn btn-sm", onclick: async () => { await api(`/api/studio/looks/${l.id}`, { method: "DELETE" }); loadSettings(); } }, "✕"))); if (!S.looks.length) ll.append(el("p", { class: "muted" }, "No looks yet."));
    } catch (e) { $("#st-msg").textContent = e.message; }
  }
  $("#look-save").addEventListener("click", async () => { const name = $("#look-name").value.trim(); if (!name) return notify("error", "Give the look a name."); await api("/api/studio/looks", { method: "PUT", body: JSON.stringify({ name, background: $("#look-bg").value, pose: $("#look-pose").value, model_asset_id: $("#look-use-model").checked ? S.lockedModel : null }) }); $("#look-name").value = ""; loadSettings(); });
  $("#st-save").addEventListener("click", async () => { try { await api("/api/studio/settings", { method: "PUT", body: JSON.stringify({ auto_pack_new_uploads: $("#st-auto").checked, clean_uploads: $("#st-clean").checked, daily_budget_eur: Number($("#st-budget").value || 0), notify_url: $("#st-notify").value.trim() }) }); await api("/api/studio/storefront", { method: "PUT", body: JSON.stringify({ revalidate_url: $("#sf-url").value.trim(), revalidate_secret: $("#sf-secret").value }) }); $("#sf-secret").value = ""; $("#st-msg").textContent = "Saved."; } catch (e) { $("#st-msg").textContent = e.message; } });

  // ---- edit details / clone (unchanged behaviour) ----------------------------------------------
  const paras = (t) => t.split(/\n\s*\n/).map((x) => x.trim()).filter(Boolean);
  $("#edit-product").addEventListener("click", async () => {
    if (!S.product) return; const dlg = $("#edit-dialog"); $("#ed-msg").textContent = "Loading…"; dlg.showModal();
    try {
      const d = await api(`/api/studio/product-details/${encodeURIComponent(S.product.id)}`);
      $("#edit-title").textContent = `Edit: ${d.name}`; $("#ed-name").value = d.name; $("#ed-slug").value = d.slug || ""; $("#ed-desc").value = d.description.join("\n\n"); $("#ed-seo-title").value = d.seo_title; $("#ed-seo-desc").value = d.seo_description;
      const cat = $("#ed-category"); cat.replaceChildren(el("option", { value: "" }, "— none —")); for (const c of d.categories) cat.append(el("option", { value: c.id }, c.path)); cat.value = d.category_id || "";
      const attrs = $("#ed-attrs"); attrs.replaceChildren(); for (const a of d.attributes) attrs.append(el("label", {}, a.name, el("input", { "data-attr": a.id, value: a.value })));
      $("#ed-name-de").value = d.translation_de.name; $("#ed-desc-de").value = d.translation_de.description.join("\n\n"); $("#ed-seo-title-de").value = d.translation_de.seo_title; $("#ed-seo-desc-de").value = d.translation_de.seo_description;
      const t = $("#ed-variants"); t.replaceChildren(); t.append(el("thead", {}, el("tr", {}, el("th", {}, "Variant"), el("th", {}, "SKU"), ...d.channels.map((c) => el("th", {}, `Price ${c.currencyCode}`)), ...d.warehouses.map((w) => el("th", {}, `Stock ${w.name}`)))));
      const tb = el("tbody"); for (const v of d.variants) tb.append(el("tr", { "data-id": v.id }, el("td", {}, v.label), el("td", {}, el("input", { "data-sku": "", value: v.sku })), ...d.channels.map((c) => el("td", {}, el("input", { class: "num", type: "number", step: "0.01", "data-ch": c.id, value: v.prices[c.id] ?? "" }))), ...d.warehouses.map((w) => el("td", {}, el("input", { class: "num", type: "number", "data-wh": w.id, value: v.stocks[w.id] ?? 0 }))))); t.append(tb); $("#ed-msg").textContent = "";
      $("#ed-save").onclick = async () => { const btn = $("#ed-save"); btn.disabled = true; try { const body = { name: $("#ed-name").value.trim(), slug: $("#ed-slug").value.trim() || null, category_id: $("#ed-category").value || null, description: paras($("#ed-desc").value), seo_title: $("#ed-seo-title").value.trim(), seo_description: $("#ed-seo-desc").value.trim(), attributes: Object.fromEntries([...attrs.querySelectorAll("input[data-attr]")].map((i) => [i.dataset.attr, i.value.trim()])), translation_de: $("#ed-name-de").value.trim() ? { name: $("#ed-name-de").value.trim(), description: paras($("#ed-desc-de").value), seo_title: $("#ed-seo-title-de").value.trim(), seo_description: $("#ed-seo-desc-de").value.trim() } : null, variants: [...tb.querySelectorAll("tr")].map((tr) => ({ id: tr.dataset.id, sku: tr.querySelector("input[data-sku]").value, prices: Object.fromEntries([...tr.querySelectorAll("input[data-ch]")].filter((i) => i.value !== "").map((i) => [i.dataset.ch, Number(i.value)])), stocks: Object.fromEntries([...tr.querySelectorAll("input[data-wh]")].map((i) => [i.dataset.wh, Number(i.value || 0)])) })) }; const r = await api(`/api/studio/product-details/${encodeURIComponent(S.product.id)}`, { method: "PUT", body: JSON.stringify(body) }); S.product.name = r.product.name; $("#product-title").textContent = r.product.name; renderProducts(); dlg.close(); notify("success", r.steps.join(", ")); } catch (e) { $("#ed-msg").textContent = e.message; } finally { btn.disabled = false; } };
    } catch (e) { $("#ed-msg").textContent = e.message; }
  });
  $("#clone-product").addEventListener("click", () => {
    if (!S.product) return; const dlg = $("#clone-dialog"); $("#clone-name").value = S.product.name + " – "; $("#clone-color").value = ""; $("#clone-suffix").value = ""; $("#clone-msg").textContent = "";
    $("#clone-go").onclick = async () => { const btn = $("#clone-go"); btn.disabled = true; try { const r = await api("/api/studio/clone", { method: "POST", body: JSON.stringify({ source_product_id: S.product.id, name: $("#clone-name").value.trim(), color: $("#clone-color").value.trim(), sku_suffix: $("#clone-suffix").value.trim(), asset_ids: [], copy_stock: $("#clone-stock").checked, copy_source_images: $("#clone-images").checked }) }); dlg.close(); notify("success", `Created "${r.product.name}"`); await loadProducts(true); } catch (e) { $("#clone-msg").textContent = e.message; } finally { btn.disabled = false; } };
    dlg.showModal();
  });

  if (qs.get("token")) { S.token = qs.get("token"); if (!S.domain) S.domain = domainFromToken(S.token); boot(); }
})();
