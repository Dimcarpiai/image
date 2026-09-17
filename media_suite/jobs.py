"""Background generation jobs: gather inputs -> provider -> store outputs as assets."""
import asyncio
import logging
import os
from typing import Dict, List, Optional

from PIL import Image
from io import BytesIO

from .crypto import decrypt
from .db import Installation, db
from .providers import PROVIDERS, GenerateRequest, ImageInput, ProviderError
from .saleor_api import SaleorAPI
from .settings import settings

logger = logging.getLogger(__name__)
_running: Dict[str, asyncio.Task] = {}
_sem = asyncio.Semaphore(3)

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4", "video/webm": "webm"}


def asset_dir(kind: str) -> str:
    path = os.path.join(settings.data_dir, kind)
    os.makedirs(path, exist_ok=True)
    return path


def save_bytes(kind: str, asset_id_hint: str, data: bytes, mime: str) -> str:
    ext = EXT.get(mime, "bin")
    path = os.path.join(asset_dir(kind), f"{asset_id_hint}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def normalize_image(data: bytes, max_side: int = 2048) -> ImageInput:
    """Downscale big inputs and convert to PNG so every provider gets the same thing."""
    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, "PNG")
    return ImageInput(buf.getvalue(), "image/png")


def provider_key(domain: str, provider_id: str) -> str:
    keys = db.get_settings(domain).get("keys", {})
    token = keys.get(provider_id)
    if not token:
        raise ProviderError(f"no API key saved for provider '{provider_id}' - add it in Settings")
    return decrypt(token)


async def _load_inputs(installation: Installation, job: dict) -> GenerateRequest:
    inp = job["input"]
    product_images: List[ImageInput] = []
    async with SaleorAPI(installation.saleor_api_url, installation.auth_token) as api:
        for url in inp.get("product_image_urls", [])[:4]:
            product_images.append(normalize_image(await api.download(url)))
    for asset_id in inp.get("source_asset_ids", [])[:4]:
        a = db.get_asset(job["domain"], asset_id)
        if a and a["mime"].startswith("image/"):
            with open(a["path"], "rb") as f:
                product_images.append(normalize_image(f.read()))
    model_image: Optional[ImageInput] = None
    if inp.get("model_asset_id"):
        a = db.get_asset(job["domain"], inp["model_asset_id"])
        if not a:
            raise ProviderError("model photo not found")
        with open(a["path"], "rb") as f:
            model_image = normalize_image(f.read())
    return GenerateRequest(mode=job["mode"], prompt=inp.get("prompt", ""), product_images=product_images,
                           model_image=model_image, options=inp.get("options", {}))


async def _run(installation: Installation, job_id: str):
    job = db.get_job(installation.domain, job_id)
    async with _sem:
        db.update_job(job_id, "running")
        try:
            req = await _load_inputs(installation, job)
            provider = PROVIDERS[job["provider"]]
            key = provider_key(installation.domain, job["provider"])
            outputs = await asyncio.wait_for(provider.generate(job["model"], req, key), timeout=settings.job_timeout)
            asset_ids = []
            for i, out in enumerate(outputs):
                hint = f"{job_id}-{i}"
                path = save_bytes("generated", hint, out.data, out.mime)
                asset = db.add_asset(installation.domain, "generated", out.mime, path, product_id=job["product_id"],
                                     label=job["input"].get("prompt", "")[:120],
                                     meta={"job_id": job_id, "mode": job["mode"], "provider": job["provider"], "model": job["model"]})
                asset_ids.append(asset["id"])
            db.update_job(job_id, "done", asset_ids=asset_ids)
        except asyncio.TimeoutError:
            db.update_job(job_id, "error", error="generation timed out")
        except ProviderError as exc:
            db.update_job(job_id, "error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("job %s failed", job_id)
            db.update_job(job_id, "error", error=f"{type(exc).__name__}: {exc}")
        finally:
            _running.pop(job_id, None)


def start_job(installation: Installation, job_id: str):
    _running[job_id] = asyncio.create_task(_run(installation, job_id))
