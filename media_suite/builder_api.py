"""Product Builder: photos -> AI draft -> variants/SKUs/prices/stock -> product in Saleor."""
import itertools
import logging
import os
import re
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .db import Installation, db
from .jobs import asset_dir, normalize_image, provider_key
from .llm import DEFAULT_MODELS, draft_product, improve_copy
from .providers.base import ProviderError
from .saleor_api import SaleorAPI, SaleorAPIError, editorjs, parse_editorjs
from .llm import DEFAULT_TEMPLATE
from .storefront import notify_storefront
from .studio_api import _asset_out, current_shop

router = APIRouter(prefix="/api/builder", tags=["builder"])


def _llm_choice(domain: str) -> tuple:
    st = db.get_settings(domain)
    keys = st.get("keys", {})
    provider = st.get("llm", {}).get("provider") or next((p for p in ("openai", "gemini", "local") if keys.get(p)), "")
    if not provider or not keys.get(provider):
        raise HTTPException(status_code=400, detail="AI drafting needs an OpenAI, Gemini or Local GPU key (AI Studio → API keys)")
    return provider, st.get("llm", {}).get("model") or DEFAULT_MODELS[provider]


def _read_asset(domain: str, asset_id: str) -> tuple:
    a = db.get_asset(domain, asset_id)
    if not a or not a["mime"].startswith("image/"):
        raise HTTPException(status_code=404, detail=f"image {asset_id} not found")
    with open(a["path"], "rb") as f:
        return a, f.read()


def make_sku(pattern: str, ctx: Dict[str, str]) -> str:
    def repl(m):
        key, mod = m.group(1), m.group(2)
        val = str(ctx.get(key, "")).strip()
        if mod == "3":
            val = val[:3]
        elif mod == "1":
            val = val[:1]
        val = re.sub(r"[^A-Za-z0-9]+", "", val)
        return val.upper()
    return re.sub(r"\{([a-zA-Z_]+)(?::(\d))?\}", repl, pattern).strip("-").upper()


# -- metadata & uploads --------------------------------------------------------
@router.get("/meta")
async def meta(shop: Installation = Depends(current_shop)):
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            data = await api.builder_meta()
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    st = db.get_settings(shop.domain)
    data["defaults"] = st.get("builder_defaults", {})
    data["copy_template"] = st.get("copy_template") or DEFAULT_TEMPLATE
    data["llm"] = {"provider": st.get("llm", {}).get("provider", ""), "model": st.get("llm", {}).get("model", ""),
                   "available": [p for p in ("openai", "gemini", "local") if st.get("keys", {}).get(p)]}
    data["storefront"] = {"revalidate_url": st.get("storefront", {}).get("revalidate_url", ""),
                          "has_secret": bool(st.get("storefront", {}).get("revalidate_secret"))}
    return data


class BuilderSettings(BaseModel):
    defaults: Optional[dict] = None
    copy_template: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    revalidate_url: Optional[str] = None
    revalidate_secret: Optional[str] = None   # "" keeps, "-" clears


@router.put("/settings")
async def put_settings(body: BuilderSettings, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    if body.defaults is not None:
        st["builder_defaults"] = {**st.get("builder_defaults", {}), **body.defaults}
    if body.copy_template is not None:
        st["copy_template"] = body.copy_template
    if body.llm_provider is not None or body.llm_model is not None:
        st["llm"] = {"provider": body.llm_provider or st.get("llm", {}).get("provider", ""), "model": (body.llm_model or "").strip()}
    if body.revalidate_url is not None:
        st.setdefault("storefront", {})["revalidate_url"] = body.revalidate_url.strip()
    if body.revalidate_secret:
        st.setdefault("storefront", {})["revalidate_secret"] = "" if body.revalidate_secret == "-" else body.revalidate_secret
    db.save_settings(shop.domain, st)
    return {"ok": True}


async def clean_background(data: bytes, api_key: str) -> bytes:
    """Stability remove-background, then composite on white and pad to a square (try-on friendly)."""
    import aiohttp
    from io import BytesIO
    from PIL import Image
    form = aiohttp.FormData()
    form.add_field("image", data, filename="in.png", content_type="image/png")
    form.add_field("output_format", "png")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post("https://api.stability.ai/v2beta/stable-image/edit/remove-background", data=form,
                          headers={"Authorization": f"Bearer {api_key}", "Accept": "image/*"}) as resp:
            if resp.status != 200:
                raise ProviderError(f"remove-background HTTP {resp.status}")
            cut = await resp.read()
    with Image.open(BytesIO(cut)).convert("RGBA") as img:
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        side = int(max(img.size) * 1.12)
        canvas = Image.new("RGB", (side, side), (255, 255, 255))
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
        out = BytesIO(); canvas.save(out, "PNG"); return out.getvalue()


@router.post("/upload")
async def upload(file: UploadFile = File(...), clean: Optional[str] = Form(None), shop: Installation = Depends(current_shop)):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="only images are accepted")
    data = normalize_image(await file.read(), 2500).data
    st = db.get_settings(shop.domain)
    do_clean = (clean == "1") if clean is not None else bool(st.get("studio", {}).get("clean_uploads"))
    cleaned = False
    if do_clean and st.get("keys", {}).get("stability"):
        try:
            data = await clean_background(data, provider_key(shop.domain, "stability")); cleaned = True
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("upload cleaning failed: %s", exc)
    path = os.path.join(asset_dir("uploads"), f"{uuid.uuid4().hex}.png")
    with open(path, "wb") as f:
        f.write(data)
    return _asset_out(db.add_asset(shop.domain, "upload", "image/png", path, label=file.filename, meta={"status": "approved", "cleaned": cleaned}))


# -- AI draft ---------------------------------------------------------------------
class DraftBody(BaseModel):
    asset_ids: List[str]
    hints: str = ""


@router.post("/draft")
async def draft(body: DraftBody, shop: Installation = Depends(current_shop)):
    if not body.asset_ids:
        raise HTTPException(status_code=400, detail="add at least one photo")
    provider, model = _llm_choice(shop.domain)
    images = [normalize_image(_read_asset(shop.domain, a)[1], 1024) for a in body.asset_ids[:4]]
    try:
        result = await draft_product(provider, model, images, body.hints, provider_key(shop.domain, provider))
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"provider": provider, "model": model, "draft": result}


# -- create -----------------------------------------------------------------------
class VariantRow(BaseModel):
    attributes: Dict[str, str]          # attribute id -> value name
    sku: str
    prices: Dict[str, float]            # channel id -> price
    stocks: Dict[str, int] = {}         # warehouse id -> quantity
    enabled: bool = True
    image_asset_id: Optional[str] = None


class CreateBody(BaseModel):
    product_type_id: str
    category_id: Optional[str] = None
    name: str
    slug: Optional[str] = None
    description_en: List[str] = []
    seo_title: str = ""
    seo_description: str = ""
    product_attributes: Dict[str, str] = {}      # attribute id -> value
    channels: List[dict] = []                    # {id, published: bool}
    variants: List[VariantRow]
    image_asset_ids: List[str] = []
    alt_text: str = ""
    translation_de: Optional[dict] = None        # {name, description: [..], seo_title, seo_description}
    weight_kg: Optional[float] = None


def _attr_inputs(values: Dict[str, str], attr_types: Dict[str, str]) -> list:
    out = []
    for attr_id, value in values.items():
        if value in (None, ""):
            continue
        t = attr_types.get(attr_id, "DROPDOWN")
        if t in ("DROPDOWN", "SWATCH"):
            out.append({"id": attr_id, "dropdown": {"value": value}})
        elif t == "MULTISELECT":
            out.append({"id": attr_id, "multiselect": [{"value": v.strip()} for v in value.split(",") if v.strip()]})
        elif t == "NUMERIC":
            out.append({"id": attr_id, "numeric": str(value)})
        elif t == "BOOLEAN":
            out.append({"id": attr_id, "boolean": str(value).lower() in ("1", "true", "yes")})
        else:
            out.append({"id": attr_id, "plainText": str(value)})
    return out


@router.post("/create")
async def create(body: CreateBody, shop: Installation = Depends(current_shop)):
    enabled = [v for v in body.variants if v.enabled]
    if not enabled:
        raise HTTPException(status_code=400, detail="enable at least one variant")
    skus = [v.sku for v in enabled if v.sku]
    if len(set(skus)) != len(skus):
        raise HTTPException(status_code=400, detail="SKUs must be unique")

    report = {"steps": []}
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            meta = await api.builder_meta()
            ptype = next((p for p in meta["productTypes"] if p["id"] == body.product_type_id), None)
            if not ptype:
                raise HTTPException(status_code=400, detail="unknown product type")
            attr_types = {a["id"]: a["inputType"] for a in ptype["productAttributes"] + ptype["variantAttributes"]}

            product_input = {
                "productType": body.product_type_id, "name": body.name.strip(),
                "description": editorjs(body.description_en),
                "attributes": _attr_inputs(body.product_attributes, attr_types),
                "seo": {"title": body.seo_title[:70], "description": body.seo_description[:300]},
            }
            if body.category_id:
                product_input["category"] = body.category_id
            if body.slug:
                product_input["slug"] = body.slug
            if body.weight_kg:
                product_input["weight"] = body.weight_kg
            product = await api.create_product(product_input)
            report["steps"].append(f"product created ({product['id']})")

            if body.channels:
                can_publish = bool(body.category_id)
                await api.update_product_channels(product["id"], [
                    {"channelId": c["id"], "isPublished": bool(c.get("published", True)) and can_publish, "visibleInListings": can_publish,
                     "isAvailableForPurchase": True} for c in body.channels])
                report["steps"].append(f"listed in {len(body.channels)} channel(s)" + ("" if can_publish else " — unpublished: choose a category to publish"))

            variant_inputs = []
            for v in enabled:
                variant_inputs.append({
                    "sku": v.sku or None,
                    "attributes": [{"id": k, "dropdown": {"value": val}} if attr_types.get(k, "DROPDOWN") in ("DROPDOWN", "SWATCH")
                                   else {"id": k, "plainText": val} for k, val in v.attributes.items() if val],
                    "trackInventory": True,
                    "channelListings": [{"channelId": ch, "price": price} for ch, price in v.prices.items() if price is not None],
                    "stocks": [{"warehouse": wh, "quantity": int(q)} for wh, q in v.stocks.items()],
                })
            created = await api.bulk_create_variants(product["id"], variant_inputs)
            report["steps"].append(f"{len(created)} variant(s) created")

            media_by_asset = {}
            for asset_id in body.image_asset_ids:
                a, data = _read_asset(shop.domain, asset_id)
                media = await api.create_media(product["id"], data, f"{asset_id[:8]}.png", "image/png", body.alt_text)
                media_by_asset[asset_id] = media["id"]
            if media_by_asset:
                report["steps"].append(f"{len(media_by_asset)} image(s) attached")
            for v, cv in zip(enabled, created):
                if v.image_asset_id and v.image_asset_id in media_by_asset:
                    await api.assign_variant_media(media_by_asset[v.image_asset_id], cv["id"])

            if body.translation_de and body.translation_de.get("name"):
                t = body.translation_de
                try:
                    await api.translate_product(product["id"], "DE", t["name"], editorjs(t.get("description", [])),
                                                t.get("seo_title", "")[:70], t.get("seo_description", "")[:300])
                    report["steps"].append("German translation saved")
                except SaleorAPIError as exc:
                    report["steps"].append("German translation NOT saved: " + ("reinstall the app to grant MANAGE_TRANSLATIONS" if "MANAGE_TRANSLATIONS" in str(exc.errors) else str(exc.errors)[:160]))
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors, "done": report["steps"]})

    await notify_storefront(shop.domain, product["id"], product.get("slug", ""), "product-created")
    return {"product": product, "variants": created, **report}


# -- copywriting for existing products (no images needed) ----------------------------
COPY_SOURCE = """
query CopySource($id: ID!) {
  product(id: $id) {
    id name slug description seoTitle seoDescription
    category { name } productType { name }
    attributes { attribute { name } values { name plainText } }
    translation(languageCode: DE) { name description seoTitle seoDescription }
    variants { id name sku attributes { attribute { name } values { name } } }
  }
}
"""


def _paras_from_editor(description_json: Optional[str]) -> List[str]:
    import json as _json, re as _re
    try:
        blocks = _json.loads(description_json or "{}").get("blocks", [])
    except _json.JSONDecodeError:
        return []
    return [_re.sub(r"<[^>]+>", "", b.get("data", {}).get("text", "")) for b in blocks if b.get("type") == "paragraph"]


@router.get("/copy/{product_id}")
async def copy_source(product_id: str, shop: Installation = Depends(current_shop)):
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        p = ((await api.execute(COPY_SOURCE, {"id": product_id})) or {}).get("product")
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    tr = p.get("translation") or {}
    return {
        "id": p["id"], "name": p["name"], "slug": p["slug"], "category": (p.get("category") or {}).get("name"), "product_type": (p.get("productType") or {}).get("name"),
        "attributes": {a["attribute"]["name"]: ", ".join(v.get("name") or v.get("plainText") or "" for v in a["values"]) for a in p["attributes"] if a["values"]},
        "intro_en": parse_editorjs(p.get("description"))["intro"], "details_en": parse_editorjs(p.get("description"))["details"],
        "seo_title_en": p.get("seoTitle") or "", "seo_description_en": p.get("seoDescription") or "",
        "name_de": tr.get("name") or "", "intro_de": parse_editorjs(tr.get("description"))["intro"], "details_de": parse_editorjs(tr.get("description"))["details"],
        "seo_title_de": tr.get("seoTitle") or "", "seo_description_de": tr.get("seoDescription") or "",
        "variants": [{"id": v["id"], "name": v.get("name") or "", "sku": v.get("sku") or "", "attributes": {a["attribute"]["name"]: ", ".join(x["name"] for x in a["values"]) for a in v["attributes"] if a["values"]}} for v in p["variants"]],
    }


class ImproveBody(BaseModel):
    product_id: str
    tone: str = "clear and premium"
    languages: List[str] = ["en", "de"]
    instructions: str = ""
    provider: Optional[str] = None
    model: Optional[str] = None


@router.post("/improve")
async def improve(body: ImproveBody, shop: Installation = Depends(current_shop)):
    current = await copy_source(body.product_id, shop)
    provider, model = (body.provider, body.model) if body.provider else _llm_choice(shop.domain)
    keys = db.get_settings(shop.domain).get("keys", {})
    if not keys.get(provider):
        raise HTTPException(status_code=400, detail=f"no API key for {provider}")
    try:
        template = db.get_settings(shop.domain).get("copy_template") or DEFAULT_TEMPLATE
        result = await improve_copy(provider, model or DEFAULT_MODELS.get(provider), current, body.tone, body.languages, body.instructions, provider_key(shop.domain, provider), template)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"provider": provider, "model": model or DEFAULT_MODELS.get(provider), "current": current, "suggestion": result}


class ApplyCopyBody(BaseModel):
    product_id: str
    name_en: Optional[str] = None
    intro_en: Optional[List[str]] = None
    details_en: Optional[List[str]] = None
    seo_title_en: Optional[str] = None
    seo_description_en: Optional[str] = None
    name_de: Optional[str] = None
    intro_de: Optional[List[str]] = None
    details_de: Optional[List[str]] = None
    details_title: str = "Product Details"
    seo_title_de: Optional[str] = None
    seo_description_de: Optional[str] = None
    variant_names: Dict[str, str] = {}


@router.post("/apply-copy")
async def apply_copy(body: ApplyCopyBody, shop: Installation = Depends(current_shop)):
    steps = []
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            inp = {}
            if body.name_en is not None: inp["name"] = body.name_en.strip()
            if body.intro_en is not None or body.details_en is not None:
                cur = parse_editorjs(current_desc) if (current_desc := (await api.execute(COPY_SOURCE, {"id": body.product_id}))["product"]["description"]) else {"intro": [], "details": []}
                inp["description"] = editorjs(body.intro_en if body.intro_en is not None else cur["intro"], body.details_en if body.details_en is not None else cur["details"], body.details_title)
            if body.seo_title_en is not None or body.seo_description_en is not None:
                inp["seo"] = {"title": (body.seo_title_en or "")[:70], "description": (body.seo_description_en or "")[:300]}
            if inp:
                await api.update_product(body.product_id, inp); steps.append("English texts updated")
            if body.name_de or body.intro_de or body.details_de:
                try:
                    await api.translate_product(body.product_id, "DE", body.name_de or "", editorjs(body.intro_de or [], body.details_de or [], "Produktdetails" if body.details_title == "Product Details" else body.details_title),
                                                (body.seo_title_de or "")[:70], (body.seo_description_de or "")[:300])
                    steps.append("German translation updated")
                except SaleorAPIError as exc:
                    steps.append("German translation NOT saved: " + ("the app lacks MANAGE_TRANSLATIONS — reinstall it to grant the permission" if "MANAGE_TRANSLATIONS" in str(exc.errors) else str(exc.errors)[:160]))
            for vid, name in body.variant_names.items():
                if name and name.strip():
                    await api.update_variant(vid, {"name": name.strip()[:255]})
            if body.variant_names:
                steps.append(f"{len(body.variant_names)} variant name(s) updated")
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors, "done": steps})
    await notify_storefront(shop.domain, body.product_id, "", "copy-updated")
    return {"steps": steps}
