"""Find candidate images on a web page (Pinterest pins, supplier pages, ...). Best effort, no JS execution."""
import re
from html import unescape
from typing import List
from urllib.parse import urljoin, urlparse

import aiohttp

from .jobs import _check_public_url
from .providers.base import ProviderError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|avif|gif)(\?|$)", re.I)


def _dedupe(urls: List[str]) -> List[str]:
    seen, out = set(), []
    for u in urls:
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key); out.append(u)
    return out


def extract_images(html: str, base_url: str) -> List[str]:
    found: List[str] = []
    # 1. social/meta images first (usually the main picture)
    for m in re.finditer(r'<meta[^>]+(?:property|name)=["\'](?:og:image|og:image:secure_url|twitter:image)["\'][^>]+content=["\']([^"\']+)', html, re.I):
        found.append(m.group(1))
    for m in re.finditer(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']', html, re.I):
        found.append(m.group(1))
    # 2. <img src / data-src / srcset>
    for m in re.finditer(r'<img[^>]+(?:src|data-src|data-lazy-src)=["\']([^"\']+)', html, re.I):
        found.append(m.group(1))
    for m in re.finditer(r'srcset=["\']([^"\']+)', html, re.I):
        parts = [p.strip().split(" ")[0] for p in m.group(1).split(",")]
        found.extend(parts[-1:])
    # 3. image URLs embedded in scripts/JSON (Pinterest, Instagram, Shopify …)
    for m in re.finditer(r'https?:\\?/\\?/(?:[^"\'\s<>\\]|\\/)+?\.(?:jpe?g|png|webp)(?:\?(?:[^"\'\s<>\\]|\\/)*)?', html, re.I):
        found.append(m.group(0).replace("\\/", "/"))
    # normalise
    out = []
    for u in found:
        u = unescape(u).strip()
        if u.startswith("data:") or not u:
            continue
        u = urljoin(base_url, u)
        if urlparse(u).scheme not in ("http", "https"):
            continue
        # skip obvious icons/trackers
        if re.search(r"(favicon|sprite|pixel|1x1|logo|icon|badge|avatar|emoji)", u, re.I) and not re.search(r"pinimg\.com/(736x|originals)", u):
            continue
        out.append(u)
    out = _dedupe(out)
    # Pinterest: prefer the large sizes and drop tiny thumbs
    if "pinimg.com" in base_url or any("pinimg.com" in u for u in out):
        big = [u for u in out if re.search(r"pinimg\.com/(originals|736x|564x)/", u)]
        out = big or out
    return out[:60]


async def fetch_page_images(url: str) -> List[str]:
    _check_public_url(url)
    if IMG_EXT.search(urlparse(url).path):
        return [url]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), headers={"User-Agent": UA, "Accept-Language": "en,de;q=0.8"}) as s:
        async with s.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise ProviderError(f"page returned HTTP {resp.status}")
            ctype = resp.headers.get("Content-Type", "")
            if ctype.startswith("image/"):
                return [str(resp.url)]
            html = await resp.text(errors="ignore")
    images = extract_images(html, str(url))
    if not images:
        raise ProviderError("no images found on that page (it may be rendered by JavaScript or blocked for bots)")
    return images
