"""Tell the storefront that a product changed (so cached pages revalidate). Configured per shop."""
import logging

import aiohttp

from .db import db

logger = logging.getLogger(__name__)


async def notify_storefront(domain: str, product_id: str, slug: str = "", reason: str = "media") -> bool:
    cfg = db.get_settings(domain).get("storefront", {})
    url = (cfg.get("revalidate_url") or "").strip()
    if not url:
        return False
    payload = {"productId": product_id, "slug": slug, "reason": reason, "secret": cfg.get("revalidate_secret", "")}
    headers = {"Content-Type": "application/json"}
    if cfg.get("revalidate_secret"):
        headers["Authorization"] = f"Bearer {cfg['revalidate_secret']}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                ok = resp.status < 300
                if not ok:
                    logger.warning("storefront revalidate %s -> HTTP %s", url, resp.status)
                return ok
    except aiohttp.ClientError as exc:
        logger.warning("storefront revalidate failed: %s", exc)
        return False
