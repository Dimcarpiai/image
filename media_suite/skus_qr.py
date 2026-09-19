"""SKU generation for existing products and QR codes for storefront URLs."""
import io
import re
from typing import Dict, List, Optional

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from .builder_api import make_sku
from .db import Installation, db
from .saleor_api import SaleorAPI, SaleorAPIError
from .studio_api import current_shop

router = APIRouter(prefix="/api/studio", tags=["skus-qr"])

VARIANTS_FOR_SKU = """
query SkuVariants($id: ID!) {
  product(id: $id) {
    id name slug
    category { name }
    attributes { attribute { name slug } values { name } }
    variants { id sku name attributes { attribute { name slug } values { name } } }
  }
}
"""


def _style_code(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    return (words[0][:4] if words else "ITEM").upper()


def _ctx_for(product: dict, variant: dict, brand: str, style: str) -> Dict[str, str]:
    ctx = {"brand": brand, "style": style or _style_code(product["name"]), "category": (product.get("category") or {}).get("name", "")}
    for a in product["attributes"] + variant["attributes"]:
        slug = (a["attribute"].get("slug") or "").lower(); nm = (a["attribute"].get("name") or "").lower()
        val = ", ".join(v["name"] for v in a["values"] if v.get("name"))
        if not val:
            continue
        key = "color" if ("colo" in slug or "colo" in nm or "farbe" in slug) else "size" if ("size" in slug or "size" in nm or "gr" in slug) else slug
        ctx[key] = val
    return ctx


class SkuBody(BaseModel):
    product_id: str
    pattern: Optional[str] = None      # default: builder default pattern
    brand: Optional[str] = None
    style: Optional[str] = None
    apply: bool = False
    only_empty: bool = False           # leave variants that already have a SKU


@router.post("/skus")
async def skus(body: SkuBody, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain)
    defaults = st.get("builder_defaults", {})
    pattern = body.pattern or defaults.get("sku_pattern") or "{brand}-{style}-{color:3}-{size}"
    brand = body.brand or defaults.get("brand") or "SKU"
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            p = ((await api.execute(VARIANTS_FOR_SKU, {"id": body.product_id})) or {}).get("product")
            if not p:
                raise HTTPException(status_code=404, detail="product not found")
            rows, seen = [], set()
            for v in p["variants"]:
                sku = make_sku(pattern, _ctx_for(p, v, brand, body.style or ""))
                base, i = sku, 2
                while sku in seen:                    # de-duplicate identical combinations
                    sku = f"{base}-{i}"; i += 1
                seen.add(sku)
                keep = body.only_empty and bool(v.get("sku"))
                rows.append({"variant_id": v["id"], "label": " / ".join(val["name"] for a in v["attributes"] for val in a["values"]) or v.get("name") or "default",
                             "current": v.get("sku") or "", "sku": v["sku"] if keep else sku, "changed": (not keep) and (v.get("sku") or "") != sku})
            applied = 0
            if body.apply:
                for r in rows:
                    if r["changed"]:
                        await api.update_variant(r["variant_id"], {"sku": r["sku"]}); applied += 1
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    return {"pattern": pattern, "brand": brand, "rows": rows, "applied": applied}


# -- QR codes -------------------------------------------------------------------------
def _qr_png(text: str, size: int = 512, label: str = "") -> bytes:
    from PIL import Image, ImageDraw
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(text); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((size, size), Image.Resampling.NEAREST)
    if label:
        canvas = Image.new("RGB", (size, size + 44), "white"); canvas.paste(img, (0, 0))
        d = ImageDraw.Draw(canvas); d.text((size // 2, size + 14), label[:60], fill="black", anchor="mm")
        img = canvas
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def product_url(domain: str, slug: str, sku: str = "") -> str:
    pattern = db.get_settings(domain).get("storefront", {}).get("product_url") or ""
    if not pattern:
        return ""
    url = pattern.replace("{slug}", slug)
    if sku:
        url = url.replace("{sku}", sku) if "{sku}" in url else f"{url}{'&' if '?' in url else '?'}variant={sku}"
    else:
        url = re.sub(r"[?&][a-zA-Z_]+=\{sku\}", "", url).replace("{sku}", "").rstrip("?&")
    return url


@router.get("/qr")
async def qr(url: str = Query(...), label: str = "", size: int = 512, shop: Installation = Depends(current_shop)):
    """QR PNG for any URL (or text)."""
    return Response(_qr_png(url, max(128, min(size, 1024)), label), media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="qr-{re.sub(r"[^a-z0-9]+", "-", label.lower())[:40] or "code"}.png"'})


@router.get("/qr/product/{product_id}")
async def qr_product(product_id: str, shop: Installation = Depends(current_shop)):
    """Product page URL + one URL per variant (SKU), with ready-made QR links; needs the storefront product URL pattern."""
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        p = ((await api.execute(VARIANTS_FOR_SKU, {"id": product_id})) or {}).get("product")
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    pattern = db.get_settings(shop.domain).get("storefront", {}).get("product_url") or ""
    base = product_url(shop.domain, p["slug"])
    items = [{"label": p["name"], "url": base, "sku": ""}] + [
        {"label": " / ".join(val["name"] for a in v["attributes"] for val in a["values"]) or v.get("name") or "default", "sku": v.get("sku") or "",
         "url": product_url(shop.domain, p["slug"], v.get("sku") or "")} for v in p["variants"]]
    return {"pattern": pattern, "slug": p["slug"], "items": items}


class StorefrontUrlBody(BaseModel):
    product_url: str   # e.g. https://rostau.de/products/{slug}  or  https://rostau.de/p/{slug}?variant={sku}


@router.put("/storefront-url")
async def put_storefront_url(body: StorefrontUrlBody, shop: Installation = Depends(current_shop)):
    st = db.get_settings(shop.domain); st.setdefault("storefront", {})["product_url"] = body.product_url.strip()
    db.save_settings(shop.domain, st)
    return {"product_url": st["storefront"]["product_url"]}


# -- add variants (colour × size) to an existing product ------------------------------
VARIANT_SETUP = """
query VariantSetup($id: ID!) {
  product(id: $id) {
    id name
    productType { id assignedVariantAttributes { attribute { id name slug inputType valueRequired choices(first: 100) { edges { node { name } } } } } }
    channelListings { channel { id name currencyCode } }
    variants { id sku attributes { attribute { id slug name } values { name } } channelListings { channel { id } price { amount } } stocks { warehouse { id } quantity } }
  }
}
"""


class AddVariantsBody(BaseModel):
    product_id: str
    colors: List[str] = []
    sizes: List[str] = []
    price: Optional[float] = None       # default: price of the first existing variant per channel
    stock: int = 0
    brand: Optional[str] = None
    style: Optional[str] = None


def _kind(attr: dict) -> str:
    t = ((attr.get("slug") or "") + " " + (attr.get("name") or "")).lower()
    if "colo" in t or "farbe" in t:
        return "color"
    if "size" in t or "gr" in t and "ss" in t:
        return "size"
    return "other"


@router.get("/variant-setup/{product_id}")
async def variant_setup(product_id: str, shop: Installation = Depends(current_shop)):
    """What the Add-variants dialog needs: colour/size attributes with known values, existing combos, warehouses."""
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        p = ((await api.execute(VARIANT_SETUP, {"id": product_id})) or {}).get("product")
        meta = await api.builder_meta()
    if not p:
        raise HTTPException(status_code=404, detail="product not found")
    attrs, others = {}, []
    for a in (p["productType"].get("assignedVariantAttributes") or []):
        at = a["attribute"]; k = _kind(at)
        entry = {"id": at["id"], "name": at["name"], "required": bool(at.get("valueRequired")), "inputType": at.get("inputType"),
                 "values": [c["node"]["name"] for c in (at.get("choices") or {}).get("edges", [])]}
        if k in ("color", "size") and k not in attrs:
            attrs[k] = entry
        else:
            others.append(entry)
    existing = []
    for v in p["variants"]:
        combo = {}
        for a in v["attributes"]:
            k = _kind(a["attribute"])
            if k in ("color", "size") and a["values"]:
                combo[k] = a["values"][0]["name"]
        existing.append({"id": v["id"], "sku": v.get("sku") or "", **combo})
    used_colors = sorted({e["color"] for e in existing if e.get("color")}); used_sizes = sorted({e["size"] for e in existing if e.get("size")})
    first = p["variants"][0] if p["variants"] else None
    price = (first["channelListings"][0]["price"]["amount"] if first and first["channelListings"] and first["channelListings"][0].get("price") else None)
    # values of the remaining (non colour/size) attributes on the first existing variant, used to fill required ones
    other_values = {}
    if first:
        for a in first["attributes"]:
            if a["values"]:
                other_values[a["attribute"]["id"]] = a["values"][0]["name"]
    return {"attributes": attrs, "others": others, "other_values": other_values, "existing": existing, "used_colors": used_colors, "used_sizes": used_sizes,
            "default_price": price, "channels": [c["channel"] for c in p["channelListings"]], "warehouses": meta["warehouses"]}


@router.post("/variants/create")
async def variants_create(body: AddVariantsBody, shop: Installation = Depends(current_shop)):
    setup = await variant_setup(body.product_id, shop)
    attrs = setup["attributes"]
    if body.colors and "color" not in attrs:
        raise HTTPException(status_code=400, detail="this product type has no colour variant attribute")
    if body.sizes and "size" not in attrs:
        raise HTTPException(status_code=400, detail="this product type has no size variant attribute")
    if attrs.get("size", {}).get("required") and not body.sizes:
        raise HTTPException(status_code=400, detail="Size is a required attribute on this product type — tick at least one size")
    if attrs.get("color", {}).get("required") and not body.colors:
        raise HTTPException(status_code=400, detail="Colour is a required attribute on this product type — tick at least one colour")
    extra_inputs = []
    for o in setup["others"]:
        val = setup["other_values"].get(o["id"]) or (o["values"][0] if o["values"] else None)
        if o["required"] and not val:
            raise HTTPException(status_code=400, detail=f"attribute '{o['name']}' is required but has no value to copy — add a variant with it in the dashboard first")
        if val and o["inputType"] in ("DROPDOWN", "SWATCH"):
            extra_inputs.append({"id": o["id"], "dropdown": {"value": val}})
        elif val and o["inputType"] == "PLAIN_TEXT":
            extra_inputs.append({"id": o["id"], "plainText": val})
    colors = body.colors or [""]; sizes = body.sizes or [""]
    have = {(e.get("color", "") or "", e.get("size", "") or "") for e in setup["existing"]}
    st = db.get_settings(shop.domain); defaults = st.get("builder_defaults", {})
    pattern = defaults.get("sku_pattern") or "{brand}-{style}-{color:3}-{size}"; brand = body.brand or defaults.get("brand") or "SKU"
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        p = ((await api.execute(VARIANT_SETUP, {"id": body.product_id})) or {}).get("product")
        style = body.style or _style_code(p["name"])
        price = body.price if body.price is not None else setup["default_price"]
        variants, skus = [], {e["sku"] for e in setup["existing"] if e["sku"]}
        for color in colors:
            for size in sizes:
                if (color, size) in have:
                    continue
                attr_inputs = list(extra_inputs)
                if color: attr_inputs.append({"id": attrs["color"]["id"], "dropdown": {"value": color}})
                if size: attr_inputs.append({"id": attrs["size"]["id"], "dropdown": {"value": size}})
                sku = make_sku(pattern, {"brand": brand, "style": style, "color": color, "size": size}); base, i = sku, 2
                while sku in skus:
                    sku = f"{base}-{i}"; i += 1
                skus.add(sku)
                variants.append({"sku": sku, "attributes": attr_inputs, "trackInventory": True,
                                 "channelListings": [{"channelId": c["id"], "price": price} for c in setup["channels"] if price is not None],
                                 "stocks": [{"warehouse": w["id"], "quantity": int(body.stock)} for w in setup["warehouses"][:1]]})
        if not variants:
            return {"created": [], "skipped": "all combinations already exist"}
        try:
            created = await api.bulk_create_variants(body.product_id, variants)
        except SaleorAPIError as exc:
            raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})
    return {"created": [{"id": c["id"], "sku": c.get("sku")} for c in created], "count": len(created)}
