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


# -- generation --------------------------------------------------------------
class GenerateBody(BaseModel):
    mode: str
    provider: str
    model: str
    product_id: str
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
    if not body.product_image_urls and not body.source_asset_ids and body.model != "search-replace":
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
    return {"media": media}
