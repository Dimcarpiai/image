"""Task-oriented AI Studio API (v1): Select product → task → references → generate → publish.

Users never see providers or prompts. This module builds the instruction from Saleor data + locks + presets,
picks a provider automatically (overridable in Advanced), and exposes Saleor publishing actions.
"""
import os
import shutil
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .db import Installation, db
from .jobs import asset_dir, start_job
from .providers import PROVIDERS, estimate, find_model
from .prompting import ALL_LOCKS, BACKGROUNDS, EDIT_ACTIONS, LOCKS, LOGO, PACKS, POSES, TASKS, build_prompt
from .saleor_api import SaleorAPI, SaleorAPIError
from .storefront import notify_storefront
from .studio_api import PRODUCT_SUMMARY, _asset_out, _budget_check, _product_summary, current_shop

router = APIRouter(prefix="/api/studio", tags=["studio-v1"])

# task -> provider mode
TASK_MODE = {"product": "scene", "model": "tryon", "variants": "edit", "edit": "edit", "video": "video", "remove_bg": "edit", "replace_bg": "scene"}
# preferred provider/model per mode, first configured wins
PREFERRED = {
    "scene": [("openai", "gpt-image-2.5-sunburst"), ("gemini", "gemini-3.1-flash-image"), ("stability", "relight")],
    "tryon": [("fal", "fal-ai/fashn/tryon/v1.6"), ("openai", "gpt-image-2.5-sunburst"), ("gemini", "gemini-3.1-flash-image"), ("local", "catvton")],
    "edit": [("openai", "gpt-image-2.5-sunburst"), ("gemini", "gemini-3.1-flash-image"), ("stability", "edit")],
    "video": [("fal", "fal-ai/kling-video/v3/turbo/pro/image-to-video"), ("stability", "svd")],
}
# providers that accept the composed instruction (others, like FASHN/CatVTON, use images only)
PROMPT_AWARE = {"openai", "gemini"}


def pick_provider(domain: str, mode: str, advanced: Optional[dict]) -> tuple:
    keys = db.get_settings(domain).get("keys", {})
    if advanced and advanced.get("provider"):
        prov = advanced["provider"]
        if not keys.get(prov):
            raise HTTPException(status_code=400, detail=f"no API key for {prov}")
        if advanced.get("model"):
            spec = find_model(prov, advanced["model"])
            if not spec or mode not in spec.modes:
                raise HTTPException(status_code=400, detail="that provider/model does not support this task")
            return prov, advanced["model"]
        preferred = next((m for pv, m in PREFERRED[mode] if pv == prov), None)
        if preferred:
            return prov, preferred
        first = next((m.id for m in PROVIDERS[prov].spec.models if mode in m.modes), None)
        if not first:
            raise HTTPException(status_code=400, detail=f"{prov} has no model for this task")
        return prov, first
    for prov, model in PREFERRED[mode]:
        if keys.get(prov) and find_model(prov, model):
            return prov, model
    raise HTTPException(status_code=400, detail=f"no provider configured for this task — add an API key in Settings")


@router.get("/tasks")
async def tasks(shop: Installation = Depends(current_shop)):
    keys = db.get_settings(shop.domain).get("keys", {})
    st = db.get_settings(shop.domain).get("studio", {})
    return {
        "tasks": [{"id": k, **v} for k, v in TASKS.items()],
        "poses": [{"id": k, "label": k.replace("closeup", "close-up").replace("pockets", "hands in pockets").capitalize()} for k in POSES],
        "backgrounds": [{"id": k, "label": k.capitalize() if k != "custom" else "Custom reference"} for k in BACKGROUNDS],
        "locks": [{"id": k, "label": k.capitalize()} for k in LOCKS],
        "logo": [{"id": "preserve", "label": "Keep existing logo exactly"}, {"id": "remove", "label": "Remove logo"}, {"id": "add", "label": "Add uploaded company logo"}],
        "packs": [{"id": k, "label": v["label"], "steps": [f"{t}: {', '.join(f'{a}={b}' for a, b in o.items() if a != 'extra')}" for t, o in v["steps"]]} for k, v in PACKS.items()],
        "edit_actions": [{"id": k, "label": k.replace("_", " ").capitalize(), "needs_value": "{value}" in v[0]} for k, v in EDIT_ACTIONS.items()]
                        + [{"id": "remove_bg", "label": "Remove background (cut-out PNG)", "needs_value": False, "task": "remove_bg"},
                           {"id": "replace_bg", "label": "Replace background (relight)", "needs_value": False, "task": "replace_bg"}],
        "providers_ready": {mode: next(((p, m) for p, m in lst if keys.get(p)), None) for mode, lst in PREFERRED.items()},
        "locked_model": st.get("locked_model"),
        "looks": st.get("looks", []),
    }


# -------------------------------------------------------------------------
# references & product data
# -------------------------------------------------------------------------
class Refs(BaseModel):
    product_urls: List[str] = []          # Saleor media urls (or own /media links)
    source_asset_id: Optional[str] = None # image to edit (variants / edit tasks)
    model_asset_id: Optional[str] = None
    fabric_asset_ids: List[str] = []
    style_url: Optional[str] = None       # any image url (another product's media, own asset link)
    logo_asset_id: Optional[str] = None


class Options(BaseModel):
    pose: str = "standing"
    background: str = "white"
    logo: str = "preserve"
    locks: List[str] = ALL_LOCKS
    angle: str = "front view"
    action: Optional[str] = None
    value: Optional[str] = None
    color: Optional[str] = None
    extra: str = ""
    motion: Optional[str] = None


class Advanced(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    n: int = 1
    quality: Optional[str] = None
    size: Optional[str] = None
    options: Dict[str, str] = {}      # model-specific parameters (cloth_type, steps, guidance, category, duration, ...)


class RunBody(BaseModel):
    task: str
    product_id: str
    refs: Refs = Refs()
    options: Options = Options()
    advanced: Advanced = Advanced()
    variant_id: Optional[str] = None
    parent_asset_id: Optional[str] = None
    look_id: Optional[str] = None
    batch: Optional[str] = None
    label: Optional[str] = None


async def _product(shop: Installation, product_id: str) -> dict:
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        node = ((await api.execute(PRODUCT_SUMMARY, {"id": product_id})) or {}).get("product")
    if not node:
        raise HTTPException(status_code=404, detail="product not found")
    return _product_summary(node)


def _apply_look(domain: str, body: RunBody):
    """Collection consistency: a look fixes model, background, pose, locks and provider for every product."""
    if not body.look_id:
        return
    look = next((l for l in db.get_settings(domain).get("studio", {}).get("looks", []) if l["id"] == body.look_id), None)
    if not look:
        raise HTTPException(status_code=404, detail="look not found")
    if look.get("model_asset_id"):
        body.refs.model_asset_id = look["model_asset_id"]
    for k in ("background", "pose", "logo"):
        if look.get(k):
            setattr(body.options, k, look[k])
    if look.get("locks"):
        body.options.locks = look["locks"]
    if look.get("provider") and look.get("model"):
        body.advanced.provider, body.advanced.model = look["provider"], look["model"]
    if look.get("size"):
        body.advanced.size = look["size"]


def _create_job(shop: Installation, product: dict, body: RunBody) -> dict:
    task = body.task
    if task not in TASK_MODE:
        raise HTTPException(status_code=400, detail="unknown task")
    mode = TASK_MODE[task]
    st = db.get_settings(shop.domain).get("studio", {})
    if body.options.background == "white" and st.get("default_background"):
        body.options.background = st["default_background"]
    if body.options.pose == "standing" and st.get("default_pose"):
        body.options.pose = st["default_pose"]
    _apply_look(shop.domain, body)
    refs = body.refs
    if mode == "tryon" and not refs.model_asset_id:
        refs.model_asset_id = st.get("locked_model") or st.get("default_models", {}).get(product.get("product_type") or "") or st.get("default_models", {}).get("*")
    if mode == "tryon" and not refs.model_asset_id:
        raise HTTPException(status_code=400, detail="choose a model photo (or Keep this model on an earlier result)")
    if task in ("variants", "edit", "remove_bg", "replace_bg") and not refs.source_asset_id:
        raise HTTPException(status_code=400, detail="choose the image to edit")
    if task in ("remove_bg", "replace_bg"):
        keys = db.get_settings(shop.domain).get("keys", {})
        if not keys.get("stability"):
            raise HTTPException(status_code=400, detail="Remove/replace background needs a Stability AI key (Settings)")
        body.advanced.provider, body.advanced.model = "stability", ("remove-bg" if task == "remove_bg" else "relight")
        refs.product_urls = []
    if task == "product" and not refs.product_urls:
        refs.product_urls = [m["url"] for m in product["media"][:1]]
    if task == "video" and not refs.product_urls and not refs.source_asset_id:
        refs.product_urls = [m["url"] for m in product["media"][:1]]

    needs_prompt = mode == "tryon" and (body.options.pose != "standing" or bool(refs.style_url) or bool(body.options.extra))
    if needs_prompt and not body.advanced.provider:
        keys = db.get_settings(shop.domain).get("keys", {})
        pref = [(pv, m) for pv, m in PREFERRED["tryon"] if pv in PROMPT_AWARE and keys.get(pv)] + [(pv, m) for pv, m in PREFERRED["tryon"] if pv not in PROMPT_AWARE and keys.get(pv)]
        provider, model = (pref[0] if pref else pick_provider(shop.domain, mode, None))
    else:
        provider, model = pick_provider(shop.domain, mode, body.advanced.dict())
    counts = {"product": len(refs.product_urls) + (1 if refs.source_asset_id and task not in ("variants", "edit") else 0),
              "model": 1 if (mode == "tryon" and refs.model_asset_id) else 0, "fabric": len(refs.fabric_asset_ids),
              "style": 1 if refs.style_url else 0, "logo": 1 if (refs.logo_asset_id and body.options.logo == "add") else 0}
    if task in ("variants", "edit"):
        counts["product"] = len(refs.product_urls)
    prompt = build_prompt(task if task not in ("remove_bg", "replace_bg") else "edit", product, body.options.dict(), counts) if task not in ("remove_bg", "replace_bg") else ""

    n = max(1, min(body.advanced.n, 4))
    options: Dict = {"n": n, "raw_prompt": provider in PROMPT_AWARE}
    if body.advanced.quality:
        options["quality"] = body.advanced.quality
    if body.advanced.size:
        options["size"] = body.advanced.size
        options["aspect_ratio"] = {"1024x1536": "3:4", "1536x1024": "4:3", "1024x1024": "1:1"}.get(body.advanced.size, "auto")
    if provider == "fal" and mode == "tryon":
        options["category"] = product.get("tryon_category", "auto")
    if provider == "stability" and model == "relight":
        options["background_prompt"] = BACKGROUNDS.get(body.options.background, BACKGROUNDS["white"]) + (" " + body.options.extra if body.options.extra else "")
    if task == "replace_bg":
        # relight takes the subject from the source image; pass it as the (only) product image
        refs.product_urls = []
    if provider == "stability" and model == "edit":
        options["search"] = {"remove_logo": "logo", "fix_collar": "collar", "fix_sleeve": "sleeve", "change_trousers": "trousers", "change_background": "background"}.get(body.options.action or "", "shirt")
    if provider == "local":
        options["cloth_type"] = {"tops": "upper", "bottoms": "lower", "one-pieces": "overall"}.get(product.get("tryon_category", "tops"), "upper")

    spec = find_model(provider, model)
    for k, v in (body.advanced.options or {}).items():          # explicit model parameters win over the automatic ones
        if spec and k in spec.options and v in spec.options[k]:
            options[k] = v
    cost = estimate(provider, model, mode, n)["cost_eur"]
    _budget_check(shop.domain, cost)
    extra_assets = list(refs.fabric_asset_ids)
    extra_urls = [refs.style_url] if refs.style_url else []
    if refs.logo_asset_id and body.options.logo == "add":
        extra_assets.append(refs.logo_asset_id)
    job = db.create_job(shop.domain, mode, provider, model, product["id"], {
        "task": task, "prompt": prompt, "product_image_urls": refs.product_urls,
        "source_first_asset_ids": [refs.source_asset_id] if refs.source_asset_id else [],
        "background": body.options.background,
        "source_asset_ids": [], "extra_ref_asset_ids": extra_assets, "extra_ref_urls": extra_urls,
        "model_asset_id": refs.model_asset_id if mode == "tryon" else None,
        "options": options, "variant_id": body.variant_id, "parent_asset_id": body.parent_asset_id or refs.source_asset_id,
        "batch": body.batch, "label": body.label or (TASKS.get(task, {"label": task.replace("_", " ").capitalize()})["label"] + (f" · {body.options.pose}" if task == "model" else "") + (f" · {body.options.color}" if body.options.color else "")),
        "look_id": body.look_id, "cost_eur": cost,
    })
    start_job(shop, job["id"])
    return {**job, "provider": provider, "model": model, "estimated_cost_eur": cost}


@router.post("/run")
async def run(body: RunBody, shop: Installation = Depends(current_shop)):
    product = await _product(shop, body.product_id)
    return _create_job(shop, product, body)


# -------------------------------------------------------------------------
# colour variants
# -------------------------------------------------------------------------
VARIANTS_QUERY = """
query StudioVariants($id: ID!) {
  product(id: $id) {
    id name
    attributes { attribute { name slug } values { name } }
    variants { id sku name attributes { attribute { name slug } values { name } } media { id url } }
    media { id url }
  }
}
"""


def _color_of(attrs: list) -> str:
    for a in attrs:
        slug = (a["attribute"].get("slug") or "").lower(); name = (a["attribute"].get("name") or "").lower()
        if "colo" in slug or "colo" in name or "farbe" in slug or "farbe" in name:
            return ", ".join(v["name"] for v in a["values"] if v.get("name"))
    return ""


@router.get("/variants/{product_id}")
async def variants(product_id: str, shop: Installation = Depends(current_shop)):
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        p = ((await api.execute(VARIANTS_QUERY, {"id": product_id})) or {}).get("product")
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    product_color = _color_of(p["attributes"])
    out = []
    for v in p["variants"]:
        out.append({"id": v["id"], "sku": v.get("sku") or "", "name": v.get("name") or "", "color": _color_of(v["attributes"]) or product_color,
                    "label": " / ".join(val["name"] for a in v["attributes"] for val in a["values"]) or v.get("name") or "default", "media": v.get("media") or []})
    colors = sorted({x["color"] for x in out if x["color"]})
    return {"product_color": product_color, "variants": out, "colors": colors}


class VariantGenBody(BaseModel):
    product_id: str
    source_asset_id: str
    colors: Optional[List[str]] = None       # default: every colour of the product's variants except the source colour
    advanced: Advanced = Advanced()


@router.post("/variants/generate")
async def variants_generate(body: VariantGenBody, shop: Installation = Depends(current_shop)):
    product = await _product(shop, body.product_id)
    vinfo = await variants(body.product_id, shop)
    colors = body.colors or [c for c in vinfo["colors"] if c.lower() != (vinfo["product_color"] or "").lower()]
    if not colors:
        raise HTTPException(status_code=400, detail="this product has no other colours in its variants; add colour values first")
    batch = uuid.uuid4().hex
    jobs = []
    for color in colors[:12]:
        vid = next((v["id"] for v in vinfo["variants"] if v["color"].lower() == color.lower()), None)
        rb = RunBody(task="variants", product_id=body.product_id, refs=Refs(source_asset_id=body.source_asset_id, product_urls=[m["url"] for m in product["media"][:1]]),
                     options=Options(color=color, locks=[l for l in ALL_LOCKS if l != "color"]), advanced=body.advanced, variant_id=vid, batch=batch, label=f"Colour variant · {color}")
        jobs.append(_create_job(shop, product, rb))
    return {"batch": batch, "jobs": jobs}


# -------------------------------------------------------------------------
# packs & bulk
# -------------------------------------------------------------------------
class PackBody(BaseModel):
    product_id: str
    pack: str = "product"
    refs: Refs = Refs()
    options: Options = Options()
    advanced: Advanced = Advanced()
    look_id: Optional[str] = None


async def _run_pack_v1(shop: Installation, product: dict, body: PackBody, batch: str) -> List[dict]:
    pack = PACKS.get(body.pack)
    if not pack:
        raise HTTPException(status_code=400, detail="unknown pack")
    jobs = []
    for task, step_opts in pack["steps"]:
        opts = body.options.copy(update={k: v for k, v in step_opts.items() if k != "extra"})
        if step_opts.get("extra"):
            opts.extra = (body.options.extra + " " + step_opts["extra"]).strip()
        rb = RunBody(task=task, product_id=product["id"], refs=body.refs.copy(), options=opts, advanced=body.advanced, look_id=body.look_id, batch=batch,
                     label=f"{pack['label']} · " + (step_opts.get("angle") or step_opts.get("pose") or step_opts.get("background") or task))
        try:
            jobs.append(_create_job(shop, product, rb))
        except HTTPException as exc:
            jobs.append({"status": "skipped", "label": rb.label, "error": str(exc.detail)})
    return jobs


@router.post("/pack")
async def pack(body: PackBody, shop: Installation = Depends(current_shop)):
    product = await _product(shop, body.product_id)
    batch = uuid.uuid4().hex
    return {"batch": batch, "jobs": await _run_pack_v1(shop, product, body, batch)}


class BulkBody(BaseModel):
    product_ids: List[str]
    task: str = "product"          # product | model | pack
    pack: str = "product"
    options: Options = Options()
    advanced: Advanced = Advanced()
    look_id: Optional[str] = None
    model_asset_id: Optional[str] = None


@router.post("/bulk")
async def bulk(body: BulkBody, shop: Installation = Depends(current_shop)):
    batch = uuid.uuid4().hex
    results = []
    for pid in body.product_ids[:50]:
        try:
            product = await _product(shop, pid)
            if not product["media"]:
                results.append({"product_id": pid, "error": "no product image"}); continue
            refs = Refs(product_urls=[product["media"][0]["url"]], model_asset_id=body.model_asset_id)
            if body.task == "pack":
                jobs = await _run_pack_v1(shop, product, PackBody(product_id=pid, pack=body.pack, refs=refs, options=body.options, advanced=body.advanced, look_id=body.look_id), batch)
            else:
                jobs = [_create_job(shop, product, RunBody(task=body.task, product_id=pid, refs=refs, options=body.options, advanced=body.advanced, look_id=body.look_id, batch=batch))]
            results.append({"product_id": pid, "product_name": product["name"], "jobs": [j.get("id") for j in jobs if j.get("id")], "skipped": [j for j in jobs if j.get("status") == "skipped"]})
        except HTTPException as exc:
            results.append({"product_id": pid, "error": str(exc.detail)})
            if exc.status_code == 402:
                break
    return {"batch": batch, "results": results, "started": sum(len(r.get("jobs", [])) for r in results)}


# -------------------------------------------------------------------------
# publishing (direct Saleor actions), model lock, versions, looks
# -------------------------------------------------------------------------
class PublishBody(BaseModel):
    asset_id: str
    action: str            # approve | add | add_variant | thumbnail | delete
    product_id: Optional[str] = None
    variant_id: Optional[str] = None


@router.post("/publish")
async def publish(body: PublishBody, shop: Installation = Depends(current_shop)):
    asset = db.get_asset(shop.domain, body.asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="image not found")
    if body.action == "delete":
        db.delete_asset(shop.domain, body.asset_id)
        try:
            os.remove(asset["path"])
        except OSError:
            pass
        return {"status": "deleted"}
    if body.action == "approve":
        db.set_asset_meta(shop.domain, body.asset_id, {"status": "approved"})
        return {"status": "approved"}
    product_id = body.product_id or asset.get("product_id")
    if not product_id:
        raise HTTPException(status_code=400, detail="no product for this image")
    if not asset["mime"].startswith("image/"):
        raise HTTPException(status_code=400, detail="videos can't be attached to Saleor products; download instead")
    with open(asset["path"], "rb") as f:
        data = f.read()
    alt = asset.get("label") or ""
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            media = await api.create_media(product_id, data, f"ai-{asset['id'][:8]}.png", asset["mime"], alt)
            if body.action == "add_variant":
                vid = body.variant_id or asset["meta"].get("variant_id")
                if not vid:
                    raise HTTPException(status_code=400, detail="choose a variant")
                await api.assign_variant_media(media["id"], vid)
            if body.action == "thumbnail":
                prod = await api.get_product(product_id)
                ids = [m["id"] for m in (prod or {}).get("media", [])]
                if media["id"] in ids:
                    await api.reorder_media(product_id, [media["id"]] + [i for i in ids if i != media["id"]])
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    db.set_asset_meta(shop.domain, body.asset_id, {"status": "approved", "media_id": media["id"], "published_to": product_id, "published_as": body.action})
    await notify_storefront(shop.domain, product_id, "", f"media-{body.action}")
    return {"status": "published", "media": media, "action": body.action}


class ModelLockBody(BaseModel):
    asset_id: str
    label: str = ""
    lock: bool = True


@router.post("/model-lock")
async def model_lock(body: ModelLockBody, shop: Installation = Depends(current_shop)):
    """Keep this model: copy a generated on-model image into the model library and use it for every future try-on."""
    st = db.get_settings(shop.domain)
    if not body.lock:
        st.setdefault("studio", {}).pop("locked_model", None); db.save_settings(shop.domain, st)
        return {"locked_model": None}
    src = db.get_asset(shop.domain, body.asset_id)
    if not src:
        raise HTTPException(status_code=404, detail="image not found")
    if src["kind"] == "model":
        model_asset = src
    else:
        path = os.path.join(asset_dir("models"), f"{uuid.uuid4().hex}.png")
        shutil.copyfile(src["path"], path)
        model_asset = db.add_asset(shop.domain, "model", src["mime"], path, label=body.label or "Locked model", meta={"status": "approved", "from_asset": src["id"], "locked": True})
    st.setdefault("studio", {})["locked_model"] = model_asset["id"]
    db.save_settings(shop.domain, st)
    return {"locked_model": model_asset["id"], "asset": _asset_out(model_asset)}


@router.get("/versions/{asset_id}")
async def versions(asset_id: str, shop: Installation = Depends(current_shop)):
    """Version chain: walk up to the root, then list every descendant, oldest first."""
    a = db.get_asset(shop.domain, asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="image not found")
    root = a
    seen = set()
    while root["meta"].get("parent_id") and root["meta"]["parent_id"] not in seen:
        seen.add(root["id"])
        parent = db.get_asset(shop.domain, root["meta"]["parent_id"])
        if not parent:
            break
        root = parent
    all_assets = db.list_assets(shop.domain, kind="generated", limit=500) + db.list_assets(shop.domain, kind="upload", limit=200)
    chain, frontier = [root], [root["id"]]
    while frontier:
        nxt = [x for x in all_assets if x["meta"].get("parent_id") in frontier and x["id"] not in {c["id"] for c in chain}]
        chain += nxt; frontier = [x["id"] for x in nxt]
    return [{**_asset_out(x), "current": x["id"] == asset_id} for x in chain]


class Look(BaseModel):
    id: Optional[str] = None
    name: str
    model_asset_id: Optional[str] = None
    background: str = "white"
    pose: str = "standing"
    logo: str = "preserve"
    locks: List[str] = ALL_LOCKS
    provider: Optional[str] = None
    model: Optional[str] = None
    size: Optional[str] = None


@router.put("/looks")
async def put_look(look: Look, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain); looks = st.setdefault("studio", {}).setdefault("looks", [])
    data = look.dict(); data["id"] = data["id"] or uuid.uuid4().hex[:8]
    st["studio"]["looks"] = [l for l in looks if l["id"] != data["id"]] + [data]
    db.save_settings(shop.domain, st)
    return data


@router.delete("/looks/{look_id}")
async def delete_look(look_id: str, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    st.setdefault("studio", {})["looks"] = [l for l in st["studio"].get("looks", []) if l["id"] != look_id]
    db.save_settings(shop.domain, st)
    return {"deleted": look_id}


@router.get("/results")
async def results(product_id: Optional[str] = None, batch: Optional[str] = None, shop: Installation = Depends(current_shop)):
    """Generated images for the product (or a batch), newest first, with job status for pending ones."""
    jobs = db.list_jobs(shop.domain, product_id=product_id, limit=100)
    if batch:
        jobs = [j for j in jobs if j["input"].get("batch") == batch]
    out = []
    for j in jobs:
        assets = [db.get_asset(shop.domain, a) for a in j["asset_ids"]]
        out.append({"job_id": j["id"], "status": j["status"], "error": j.get("error"), "label": j["input"].get("label") or j["input"].get("preset") or j["mode"],
                    "task": j["input"].get("task"), "variant_id": j["input"].get("variant_id"), "batch": j["input"].get("batch"), "created_at": j["created_at"],
                    "assets": [_asset_out(a) for a in assets if a]})
    return out
