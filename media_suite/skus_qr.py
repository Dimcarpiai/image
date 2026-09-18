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
