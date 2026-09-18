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
from .llm import DEFAULT_MODELS, draft_product
from .providers.base import ProviderError
from .saleor_api import SaleorAPI, SaleorAPIError, editorjs
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
    data["llm"] = {"provider": st.get("llm", {}).get("provider", ""), "model": st.get("llm", {}).get("model", ""),
                   "available": [p for p in ("openai", "gemini", "local") if st.get("keys", {}).get(p)]}
    data["storefront"] = {"revalidate_url": st.get("storefront", {}).get("revalidate_url", ""),
                          "has_secret": bool(st.get("storefront", {}).get("revalidate_secret"))}
    return data


class BuilderSettings(BaseModel):
    defaults: Optional[dict] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    revalidate_url: Optional[str] = None
    revalidate_secret: Optional[str] = None   # "" keeps, "-" clears


@router.put("/settings")
async def put_settings(body: BuilderSettings, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    if body.defaults is not None:
        st["builder_defaults"] = body.defaults
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
                await api.update_product_channels(product["id"], [
                    {"channelId": c["id"], "isPublished": bool(c.get("published", True)), "visibleInListings": True,
                     "isAvailableForPurchase": True} for c in body.channels])
                report["steps"].append(f"listed in {len(body.channels)} channel(s)")

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
                await api.translate_product(product["id"], "DE", t["name"], editorjs(t.get("description", [])),
                                            t.get("seo_title", "")[:70], t.get("seo_description", "")[:300])
                report["steps"].append("German translation saved")
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors, "done": report["steps"]})

    await notify_storefront(shop.domain, product["id"], product.get("slug", ""), "product-created")
    return {"product": product, "variants": created, **report}
