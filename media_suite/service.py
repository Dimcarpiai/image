"""Orchestration: fetch media from Saleor -> optimize -> upload -> replace."""
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel

from .db import Installation, db
from .optimizer import OptimizeSettings, optimize_image
from .saleor_api import SaleorAPI, SaleorAPIError

logger = logging.getLogger(__name__)


class MediaResult(BaseModel):
    media_id: str
    new_media_id: Optional[str] = None
    status: str  # optimized | skipped | error
    reason: Optional[str] = None
    original_bytes: int = 0
    optimized_bytes: int = 0
    saved_percent: float = 0.0
    format: Optional[str] = None
    new_size: Optional[List[int]] = None


class ProductResult(BaseModel):
    product_id: str
    product_name: str = ""
    results: List[MediaResult] = []

    @property
    def saved_bytes(self) -> int:
        return sum(r.original_bytes - r.optimized_bytes for r in self.results if r.status == "optimized")


def _basename(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1] or "image"
    name = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name)[:80] or "image"


async def optimize_product(
    installation: Installation,
    product_id: str,
    cfg: OptimizeSettings,
    media_ids: Optional[List[str]] = None,
    force: bool = False,
) -> ProductResult:
    """Optimize the images of one product.

    Each image becomes a new ProductMedia (so nothing is lost if upload fails),
    is tagged in private metadata, and - when ``replace_original`` is set - the
    original is deleted and the new media moved into its position.
    """
    async with SaleorAPI(installation.saleor_api_url, installation.auth_token) as api:
        product = await api.get_product(product_id)
        if not product:
            raise SaleorAPIError(f"product {product_id} not found")

        out = ProductResult(product_id=product_id, product_name=product["name"])
        order = [m["id"] for m in product["media"]]
        wanted = set(media_ids) if media_ids else None

        for media in product["media"]:
            mid = media["id"]
            if wanted is not None and mid not in wanted:
                continue
            if media["type"] != "IMAGE":
                out.results.append(MediaResult(media_id=mid, status="skipped", reason="not an image"))
                continue
            if media.get("optimized") == "true" and not force:
                out.results.append(MediaResult(media_id=mid, status="skipped", reason="already optimized"))
                continue

            try:
                result = await _optimize_one(api, product_id, media, cfg, order)
            except Exception as exc:  # noqa: BLE001 - keep going with the other images
                logger.exception("optimizing %s failed", mid)
                detail = getattr(exc, "errors", None) or str(exc)
                out.results.append(MediaResult(media_id=mid, status="error", reason=str(detail)))
                continue
            out.results.append(result)
            if result.status == "optimized":
                db.log_optimization(
                    installation.domain, product_id, mid, result.new_media_id,
                    result.original_bytes, result.optimized_bytes,
                )
        return out


async def _optimize_one(api: SaleorAPI, product_id: str, media: dict, cfg: OptimizeSettings, order: List[str]) -> MediaResult:
    mid = media["id"]
    original = await api.download(media["url"])
    result = optimize_image(original, cfg)

    if result.skipped_reason:
        return MediaResult(
            media_id=mid, status="skipped", reason=result.skipped_reason,
            original_bytes=result.original_bytes, optimized_bytes=result.original_bytes,
        )

    filename = f"{_basename(media['url'])}-optimized.{result.extension}"
    new_media = await api.create_media(product_id, result.data, filename, result.content_type, media.get("alt") or "")
    new_id = new_media["id"]
    await api.mark_optimized(new_id, result.original_bytes, mid)

    if cfg.replace_original:
        await api.delete_media(mid)
        # Put the new image where the old one was.
        idx = order.index(mid)
        order[idx] = new_id
        await api.reorder_media(product_id, list(order))
    else:
        order.append(new_id)

    return MediaResult(
        media_id=mid,
        new_media_id=new_id,
        status="optimized",
        original_bytes=result.original_bytes,
        optimized_bytes=result.optimized_bytes,
        saved_percent=result.saved_percent,
        format=result.format,
        new_size=list(result.new_size),
    )
