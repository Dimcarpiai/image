"""JSON API consumed by the dashboard page (static/app.js).

Every request carries the headers the framework expects:
  X-Saleor-Domain: <shop domain>     X-Saleor-Token: <staff JWT from AppBridge>
The framework validates the token against Saleor (tokenVerify); we additionally
check the staff user has MANAGE_PRODUCTS.
"""
import base64
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from saleor_app.deps import ConfigurationDataDeps

from .db import Installation, db
from .optimizer import OptimizeSettings, avif_supported, optimize_image
from .saleor_api import SaleorAPI, SaleorAPIError
from .service import ProductResult, optimize_product
from .storefront import notify_storefront

router = APIRouter(prefix="/api/optimizer", tags=["optimizer"])


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


async def current_shop(deps: ConfigurationDataDeps = Depends()) -> Installation:
    claims = _jwt_claims(deps.token)
    perms = set(claims.get("permissions") or [])
    if claims and "MANAGE_PRODUCTS" not in perms:
        raise HTTPException(status_code=403, detail="MANAGE_PRODUCTS permission required")
    installation = db.get_installation(deps.saleor_domain)
    if not installation:
        raise HTTPException(status_code=404, detail="App is not installed for this Saleor instance")
    return installation


# -- settings ---------------------------------------------------------------
@router.get("/settings", response_model=OptimizeSettings)
async def get_settings(shop: Installation = Depends(current_shop)):
    return db.get_optimize_settings(shop.domain)


@router.put("/settings", response_model=OptimizeSettings)
async def put_settings(data: OptimizeSettings, shop: Installation = Depends(current_shop)):
    db.save_optimize_settings(shop.domain, data)
    return data


@router.get("/capabilities")
async def capabilities(shop: Installation = Depends(current_shop)):
    return {"avif": avif_supported(), "stats": db.stats(shop.domain)}


# -- products ---------------------------------------------------------------
@router.get("/products")
async def products(
    after: Optional[str] = None,
    search: str = "",
    first: int = 20,
    shop: Installation = Depends(current_shop),
):
    first = max(1, min(first, 50))
    try:
        async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
            page = await api.list_products(first=first, after=after, search=search)
            urls = [
                m["url"]
                for e in page["edges"]
                for m in e["node"]["media"]
                if m["type"] == "IMAGE"
            ]
            sizes = await api.head_sizes(urls)
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})

    items = []
    for edge in page["edges"]:
        node = edge["node"]
        media = [
            {
                "id": m["id"],
                "alt": m["alt"],
                "type": m["type"],
                "url": m["url"],
                "thumb": m["thumb"],
                "optimized": m.get("optimized") == "true",
                "bytes": sizes.get(m["url"]),
            }
            for m in node["media"]
        ]
        items.append({"id": node["id"], "name": node["name"], "media": media})
    return {
        "items": items,
        "totalCount": page["totalCount"],
        "hasNextPage": page["pageInfo"]["hasNextPage"],
        "endCursor": page["pageInfo"]["endCursor"],
    }


# -- optimize ---------------------------------------------------------------
class OptimizeRequest(BaseModel):
    product_id: str
    media_ids: Optional[List[str]] = None
    force: bool = False
    settings: Optional[OptimizeSettings] = None  # override saved settings for this run


@router.post("/optimize", response_model=ProductResult)
async def optimize(req: OptimizeRequest, shop: Installation = Depends(current_shop)):
    cfg = req.settings or db.get_optimize_settings(shop.domain)
    try:
        result = await optimize_product(shop, req.product_id, cfg, req.media_ids, req.force)
        if any(r.status == "optimized" for r in result.results):
            await notify_storefront(shop.domain, req.product_id, "", "media-optimized")
        return result
    except SaleorAPIError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc), "errors": exc.errors})


class PreviewRequest(BaseModel):
    url: str
    settings: Optional[OptimizeSettings] = None


@router.post("/preview")
async def preview(req: PreviewRequest, shop: Installation = Depends(current_shop)):
    """Return the optimized bytes for one image without touching Saleor, so the
    dashboard can show a before/after with the real numbers."""
    cfg = req.settings or db.get_optimize_settings(shop.domain)
    async with SaleorAPI(shop.saleor_api_url, shop.auth_token) as api:
        try:
            original = await api.download(req.url)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not download image: {exc}")
    result = optimize_image(original, cfg)
    headers = {
        "X-Original-Bytes": str(result.original_bytes),
        "X-Optimized-Bytes": str(result.optimized_bytes),
        "X-Original-Size": f"{result.original_size[0]}x{result.original_size[1]}",
        "X-New-Size": f"{result.new_size[0]}x{result.new_size[1]}",
        "X-Skipped": result.skipped_reason or "",
        "Cache-Control": "no-store",
    }
    return Response(content=result.data, media_type=result.content_type, headers=headers)
