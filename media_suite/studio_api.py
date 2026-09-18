"""JSON API for the dashboard page."""
import base64
import json
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from saleor_app.deps import ConfigurationDataDeps

from .crypto import encrypt, sign, verify
from .db import Installation, db
from .jobs import asset_dir, start_job
from .providers import PROVIDERS, catalog, find_model
from .saleor_api import SaleorAPI, SaleorAPIError
from .storefront import notify_storefront
from .clone import clone_product, _attr_input
from .saleor_api import editorjs
from .scrape import fetch_page_images
from .jobs import normalize_image, _check_public_url
from .providers.base import ProviderError

router = APIRouter(prefix="/api/studio", tags=["studio"])


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


async def current_shop(deps: ConfigurationDataDeps = Depends()) -> Installation:
    claims = _jwt_claims(deps.token)
    if claims and "MANAGE_PRODUCTS" not in set(claims.get("permissions") or []):
        raise HTTPException(status_code=403, detail="MANAGE_PRODUCTS permission required")
    inst = db.get_installation(deps.saleor_domain)
    if not inst:
        raise HTTPException(status_code=404, detail="App is not installed for this Saleor instance")
    return inst


def _asset_url(asset: dict) -> str:
    return f"/media/{asset['id']}?sig={sign(asset['id'])}"


def _asset_out(asset: dict) -> dict:
    return {k: v for k, v in asset.items() if k not in ("path", "domain")} | {"url": _asset_url(asset)}


# -- catalog & settings ------------------------------------------------------
@router.get("/catalog")
async def get_catalog(shop: Installation = Depends(current_shop)):
    keys = db.get_settings(shop.domain).get("keys", {})
    return catalog({pid: bool(keys.get(pid)) for pid in PROVIDERS})


class KeyUpdate(BaseModel):
    provider: str
    api_key: str  # empty string removes the key


@router.put("/keys")
async def put_key(body: KeyUpdate, shop: Installation = Depends(current_shop)):
    if body.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="unknown provider")
    data = db.get_settings(shop.domain)
    keys = data.setdefault("keys", {})
    if body.api_key.strip():
        keys[body.provider] = encrypt(body.api_key.strip())
    else:
        keys.pop(body.provider, None)
    db.save_settings(shop.domain, data)
    return {"provider": body.provider, "configured": body.provider in keys}


# -- products ----------------------------------------------------------------
@router.get("/products")
async def products(after: Optional[str] = None, search: str = "", first: int = 20, shop: Installation = Depends(current_shop)):
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            page = await api.list_products_studio(first=max(1, min(first, 50)), after=after, search=search)
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    items = []
    for e in page["edges"]:
        n = e["node"]
        items.append({"id": n["id"], "name": n["name"], "category": (n.get("category") or {}).get("name"),
                      "thumbnail": (n.get("thumbnail") or {}).get("url"),
                      "media": [m for m in n["media"] if m["type"] == "IMAGE"]})
    return {"items": items, "totalCount": page["totalCount"], "hasNextPage": page["pageInfo"]["hasNextPage"], "endCursor": page["pageInfo"]["endCursor"]}


# -- assets (model photos + generated) ---------------------------------------
@router.get("/assets")
async def list_assets(kind: Optional[str] = None, product_id: Optional[str] = None, shop: Installation = Depends(current_shop)):
    return [_asset_out(a) for a in db.list_assets(shop.domain, kind=kind, product_id=product_id)]


@router.post("/assets/models")
async def upload_model_photo(file: UploadFile = File(...), label: str = Form(""), shop: Installation = Depends(current_shop)):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="only images are accepted")
    data = await file.read()
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(file.content_type, "png")
    path = os.path.join(asset_dir("models"), f"{uuid.uuid4().hex}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    asset = db.add_asset(shop.domain, "model", file.content_type, path, label=label or file.filename)
    return _asset_out(asset)


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str, shop: Installation = Depends(current_shop)):
    asset = db.delete_asset(shop.domain, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="not found")
    try:
        os.remove(asset["path"])
    except OSError:
        pass
    return {"deleted": asset_id}


# Served to <img>/<video> tags inside the dashboard iframe: signed URL, no token header possible.
media_router = APIRouter()


@media_router.get("/media/{asset_id}", include_in_schema=False)
async def media(asset_id: str, sig: str = Query(...)):
    if not verify(asset_id, sig):
        raise HTTPException(status_code=403, detail="invalid or expired link")
    # asset ids are globally unique; look it up without domain scoping
    row = db._conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()  # noqa: SLF001
    if not row or not os.path.exists(row["path"]):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(row["path"], media_type=row["mime"], headers={"Cache-Control": "private, max-age=3600"})


# -- review bucket -------------------------------------------------------------
@router.get("/review")
async def review_queue(shop: Installation = Depends(current_shop)):
    """Generated images that nobody has approved or rejected yet, newest first, with product names."""
    pending = [a for a in db.list_assets(shop.domain, kind="generated", limit=200) if a["meta"].get("status", "review") == "review"]
    names = {}
    ids = {a["product_id"] for a in pending if a.get("product_id")}
    if ids:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            for pid in ids:
                try:
                    p = await api.get_product(pid)
                    names[pid] = p["name"] if p else "(deleted product)"
                except SaleorAPIError:
                    names[pid] = pid
    return [{**_asset_out(a), "product_name": names.get(a.get("product_id"), "")} for a in pending]


class ReviewBody(BaseModel):
    asset_id: str
    decision: str     # approve | reject


@router.post("/review")
async def review(body: ReviewBody, shop: Installation = Depends(current_shop)):
    asset = db.get_asset(shop.domain, body.asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset not found")
    if body.decision == "reject":
        db.delete_asset(shop.domain, body.asset_id)
        try:
            os.remove(asset["path"])
        except OSError:
            pass
        return {"asset_id": body.asset_id, "status": "rejected"}
    if body.decision != "approve":
        raise HTTPException(status_code=400, detail="decision must be approve or reject")
    if not asset.get("product_id"):
        raise HTTPException(status_code=400, detail="asset has no product")
    result = await attach(AttachBody(asset_id=body.asset_id, product_id=asset["product_id"], alt=asset.get("label") or ""), shop)
    return {"asset_id": body.asset_id, "status": "approved", **result}


# -- presets & packs ------------------------------------------------------------
DEFAULT_PRESETS = [
    {"id": "studio", "name": "Studio white", "mode": "scene", "provider": "stability", "model": "relight",
     "prompt": "clean white studio background, soft even light from above, subtle floor shadow", "options": {"n": 1}, "in_pack": True},
    {"id": "lifestyle", "name": "Lifestyle scene", "mode": "scene", "provider": "openai", "model": "gpt-image-2.5-sunburst",
     "prompt": "on a wooden table in a bright Scandinavian living room, morning light, shallow depth of field", "options": {"n": 1, "size": "1024x1536"}, "in_pack": True},
    {"id": "onmodel", "name": "On model (FASHN)", "mode": "tryon", "provider": "fal", "model": "fal-ai/fashn/tryon/v1.6",
     "prompt": "", "options": {"n": 2, "category": "auto"}, "in_pack": True},
    {"id": "clip", "name": "Turntable clip", "mode": "video", "provider": "fal", "model": "fal-ai/kling-video/v3/turbo/pro/image-to-video",
     "prompt": "slow 360 degree turntable of the product, soft studio light, seamless", "options": {"n": 1, "duration": "5"}, "in_pack": False},
]


@router.get("/presets")
async def get_presets(shop: Installation = Depends(current_shop)):
    return db.get_settings(shop.domain).get("presets") or DEFAULT_PRESETS


@router.put("/presets")
async def put_presets(presets: List[dict], shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    st["presets"] = presets
    db.save_settings(shop.domain, st)
    return presets


class PackBody(BaseModel):
    product_id: str
    product_image_urls: List[str] = []
    model_asset_id: Optional[str] = None
    preset_ids: Optional[List[str]] = None    # default: all presets with in_pack


@router.post("/pack")
async def pack(body: PackBody, shop: Installation = Depends(current_shop)):
    presets = db.get_settings(shop.domain).get("presets") or DEFAULT_PRESETS
    chosen = [p for p in presets if (body.preset_ids and p["id"] in body.preset_ids) or (not body.preset_ids and p.get("in_pack"))]
    keys = db.get_settings(shop.domain).get("keys", {})
    started, skipped = [], []
    for p in chosen:
        if not keys.get(p["provider"]):
            skipped.append({"preset": p["name"], "reason": f"no {p['provider']} key"}); continue
        if p["mode"] == "tryon" and not body.model_asset_id:
            skipped.append({"preset": p["name"], "reason": "no model photo selected"}); continue
        if not find_model(p["provider"], p["model"]):
            skipped.append({"preset": p["name"], "reason": "unknown model"}); continue
        job = db.create_job(shop.domain, p["mode"], p["provider"], p["model"], body.product_id, {
            "prompt": p.get("prompt", ""), "product_image_urls": body.product_image_urls, "source_asset_ids": [],
            "model_asset_id": body.model_asset_id if p["mode"] == "tryon" else None,
            "options": {**p.get("options", {}), "n": max(1, min(int(p.get("options", {}).get("n", 1)), 4))}, "preset": p["name"]})
        start_job(shop, job["id"])
        started.append(job["id"])
    return {"started": started, "skipped": skipped}


# -- generation --------------------------------------------------------------
class GenerateBody(BaseModel):
    mode: str
    provider: str
    model: str
    product_id: Optional[str] = None
    prompt: str = ""
    product_image_urls: List[str] = []
    source_asset_ids: List[str] = []       # use previously generated images as input
    model_asset_id: Optional[str] = None
    options: dict = {}


@router.post("/generate")
async def generate(body: GenerateBody, shop: Installation = Depends(current_shop)):
    spec = find_model(body.provider, body.model)
    if not spec:
        raise HTTPException(status_code=400, detail="unknown provider/model")
    if body.mode not in spec.modes:
        raise HTTPException(status_code=400, detail=f"{spec.label} does not support mode '{body.mode}'")
    if not db.get_settings(shop.domain).get("keys", {}).get(body.provider):
        raise HTTPException(status_code=400, detail=f"no API key saved for {body.provider}")
    if body.mode != "model" and not body.product_image_urls and not body.source_asset_ids and body.model != "search-replace":
        raise HTTPException(status_code=400, detail="select at least one product image")
    if body.mode == "tryon" and not body.model_asset_id:
        raise HTTPException(status_code=400, detail="select a model photo")
    n = max(1, min(int(body.options.get("n", 1)), 4))
    job = db.create_job(shop.domain, body.mode, body.provider, body.model, body.product_id, {
        "prompt": body.prompt, "product_image_urls": body.product_image_urls, "source_asset_ids": body.source_asset_ids,
        "model_asset_id": body.model_asset_id, "options": {**body.options, "n": n},
    })
    start_job(shop, job["id"])
    return job


@router.get("/jobs")
async def jobs(product_id: Optional[str] = None, shop: Installation = Depends(current_shop)):
    return [_job_out(shop, j) for j in db.list_jobs(shop.domain, product_id=product_id)]


@router.get("/jobs/{job_id}")
async def job(job_id: str, shop: Installation = Depends(current_shop)):
    j = db.get_job(shop.domain, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    return _job_out(shop, j)


def _job_out(shop: Installation, j: dict) -> dict:
    assets = [db.get_asset(shop.domain, a) for a in j["asset_ids"]]
    return {**j, "assets": [_asset_out(a) for a in assets if a]}


# -- push a generated image to the product ----------------------------------
class AttachBody(BaseModel):
    asset_id: str
    product_id: str
    alt: str = ""


@router.post("/attach")
async def attach(body: AttachBody, shop: Installation = Depends(current_shop)):
    asset = db.get_asset(shop.domain, body.asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset not found")
    if not asset["mime"].startswith("image/"):
        raise HTTPException(status_code=400, detail="Saleor product media accepts images only; download the video instead")
    with open(asset["path"], "rb") as f:
        data = f.read()
    ext = asset["path"].rsplit(".", 1)[-1]
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            media = await api.create_media(body.product_id, data, f"ai-studio-{asset['id'][:8]}.{ext}", asset["mime"], body.alt)
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    db.set_asset_meta(shop.domain, asset["id"], {"status": "approved", "media_id": media["id"]})
    await notify_storefront(shop.domain, body.product_id, "", "media-attached")
    return {"media": media}


# -- clone product with generated images (colourway) --------------------------
class CloneBody(BaseModel):
    source_product_id: str
    name: str
    color: str = ""
    sku_suffix: str = ""
    asset_ids: List[str] = []
    copy_stock: bool = False
    copy_source_images: bool = False


@router.post("/clone")
async def clone(body: CloneBody, shop: Installation = Depends(current_shop)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    images = []
    for aid in body.asset_ids:
        a = db.get_asset(shop.domain, aid)
        if not a or not a["mime"].startswith("image/"):
            raise HTTPException(status_code=404, detail=f"image {aid} not found")
        with open(a["path"], "rb") as f:
            images.append((f.read(), a["mime"]))
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            result = await clone_product(api, body.source_product_id, body.name.strip(), body.color.strip(), body.sku_suffix.strip(),
                                         images, body.copy_stock, alt=f"{body.name.strip()} {body.color.strip()}".strip(),
                                         copy_source_images=body.copy_source_images)
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    for aid in body.asset_ids:
        db.set_asset_meta(shop.domain, aid, {"status": "approved", "cloned_to": result["product"]["id"]})
    await notify_storefront(shop.domain, result["product"]["id"], result["product"].get("slug", ""), "product-cloned")
    return result


# -- import images from a web page / URL ---------------------------------------
class PageBody(BaseModel):
    url: str


@router.post("/page-images")
async def page_images(body: PageBody, shop: Installation = Depends(current_shop)):
    try:
        return {"images": await fetch_page_images(body.url.strip())}
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"could not fetch page: {exc}")


class ImportBody(BaseModel):
    urls: List[str]
    label: str = ""


@router.post("/import-urls")
async def import_urls(body: ImportBody, shop: Installation = Depends(current_shop)):
    """Download external images into the app (as 'upload' assets) so they can be attached or used for cloning."""
    import aiohttp
    from .scrape import UA
    out, errors = [], []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": UA}) as s:
        for url in body.urls[:20]:
            try:
                _check_public_url(url)
                async with s.get(url) as resp:
                    if resp.status != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
                        raise ProviderError(f"HTTP {resp.status} / {resp.headers.get('Content-Type', '')}")
                    data = await resp.read()
                img = normalize_image(data, 2500)
                path = os.path.join(asset_dir("uploads"), f"{uuid.uuid4().hex}.png")
                with open(path, "wb") as f:
                    f.write(img.data)
                out.append(_asset_out(db.add_asset(shop.domain, "upload", "image/png", path, label=body.label or url.rsplit("/", 1)[-1][:80],
                                                  meta={"status": "approved", "source_url": url})))
            except Exception as exc:  # noqa: BLE001
                errors.append({"url": url, "error": str(exc)[:200]})
    return {"assets": out, "errors": errors}


# -- remove an image from a product ---------------------------------------------
class RemoveMediaBody(BaseModel):
    product_id: str
    media_id: str


@router.post("/remove-media")
async def remove_media(body: RemoveMediaBody, shop: Installation = Depends(current_shop)):
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            await api.delete_media(body.media_id)
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    await notify_storefront(shop.domain, body.product_id, "", "media-removed")
    return {"removed": body.media_id}


# -- product editor -----------------------------------------------------------------
def _paragraphs(description_json: Optional[str]) -> List[str]:
    try:
        blocks = json.loads(description_json or "{}").get("blocks", [])
    except json.JSONDecodeError:
        return []
    import re as _re
    return [_re.sub(r"<[^>]+>", "", b.get("data", {}).get("text", "")) for b in blocks if b.get("type") == "paragraph"]


@router.get("/product-details/{product_id}")
async def product_details(product_id: str, shop: Installation = Depends(current_shop)):
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            p = await api.product_edit(product_id)
            meta = await api.builder_meta()
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    ptype = next((t for t in meta["productTypes"] if t["id"] == p["productType"]["id"]), {"productAttributes": []})
    current = {a["attribute"]["id"]: ", ".join(v.get("name") or v.get("plainText") or "" for v in a["values"]) for a in p["attributes"]}
    tr = p.get("translation") or {}
    return {
        "id": p["id"], "name": p["name"], "slug": p["slug"], "category_id": (p.get("category") or {}).get("id"),
        "description": _paragraphs(p.get("description")), "seo_title": p.get("seoTitle") or "", "seo_description": p.get("seoDescription") or "",
        "attributes": [{**a, "value": current.get(a["id"], "")} for a in ptype["productAttributes"]],
        "translation_de": {"name": tr.get("name") or "", "description": _paragraphs(tr.get("description")), "seo_title": tr.get("seoTitle") or "", "seo_description": tr.get("seoDescription") or ""},
        "channels": meta["channels"], "warehouses": meta["warehouses"], "categories": meta["categories"],
        "variants": [{"id": v["id"], "sku": v.get("sku") or "", "label": " / ".join(val["name"] for a in v["attributes"] for val in a["values"]) or v.get("name") or "default",
                      "prices": {c["channel"]["id"]: c["price"]["amount"] for c in v["channelListings"] if c.get("price")},
                      "stocks": {s["warehouse"]["id"]: s["quantity"] for s in v["stocks"]}} for v in p["variants"]],
    }


class VariantEdit(BaseModel):
    id: str
    sku: str = ""
    prices: dict = {}
    stocks: dict = {}


class ProductEdit(BaseModel):
    name: str
    slug: Optional[str] = None
    category_id: Optional[str] = None
    description: List[str] = []
    seo_title: str = ""
    seo_description: str = ""
    attributes: dict = {}                 # attribute id -> value (comma separated for multiselect)
    translation_de: Optional[dict] = None
    variants: List[VariantEdit] = []


@router.put("/product-details/{product_id}")
async def update_product_details(product_id: str, body: ProductEdit, shop: Installation = Depends(current_shop)):
    steps = []
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            meta = await api.builder_meta()
            types = {a["id"]: a["inputType"] for t in meta["productTypes"] for a in t["productAttributes"]}
            attrs = []
            for aid, val in body.attributes.items():
                if val in (None, ""):
                    continue
                fake = {"attribute": {"id": aid, "name": "", "inputType": types.get(aid, "DROPDOWN")}, "values": [{"name": v.strip()} for v in str(val).split(",") if v.strip()]}
                x = _attr_input(fake, {})
                if x:
                    attrs.append(x)
            inp = {"name": body.name.strip(), "description": editorjs(body.description), "attributes": attrs,
                   "seo": {"title": body.seo_title[:70], "description": body.seo_description[:300]}}
            if body.slug:
                inp["slug"] = body.slug.strip()
            if body.category_id:
                inp["category"] = body.category_id
            product = await api.update_product(product_id, inp)
            steps.append("product updated")
            if body.translation_de and body.translation_de.get("name"):
                t = body.translation_de
                await api.translate_product(product_id, "DE", t["name"], editorjs(t.get("description", [])), t.get("seo_title", "")[:70], t.get("seo_description", "")[:300])
                steps.append("German translation saved")
            for v in body.variants:
                if v.sku is not None:
                    await api.update_variant(v.id, {"sku": v.sku.strip() or None})
                if v.prices:
                    await api.update_variant_prices(v.id, [{"channelId": ch, "price": float(pr)} for ch, pr in v.prices.items() if pr not in (None, "")])
                if v.stocks:
                    await api.update_variant_stocks(v.id, [{"warehouse": wh, "quantity": int(q)} for wh, q in v.stocks.items()])
            if body.variants:
                steps.append(f"{len(body.variants)} variant(s) updated")
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors, "done": steps})
    await notify_storefront(shop.domain, product_id, product.get("slug", ""), "product-updated")
    return {"product": product, "steps": steps}


# -- storefront revalidation settings (moved here from Product Builder) -----------
class StorefrontSettings(BaseModel):
    revalidate_url: str = ""
    revalidate_secret: str = ""     # empty keeps the saved one; "-" clears it


@router.get("/storefront")
async def get_storefront(shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain).get("storefront", {})
    return {"revalidate_url": st.get("revalidate_url", ""), "has_secret": bool(st.get("revalidate_secret"))}


@router.put("/storefront")
async def put_storefront(body: StorefrontSettings, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    sf = st.setdefault("storefront", {})
    sf["revalidate_url"] = body.revalidate_url.strip()
    if body.revalidate_secret:
        sf["revalidate_secret"] = "" if body.revalidate_secret == "-" else body.revalidate_secret
    db.save_settings(shop.domain, st)
    return {"revalidate_url": sf["revalidate_url"], "has_secret": bool(sf.get("revalidate_secret"))}
