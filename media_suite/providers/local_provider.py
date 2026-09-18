"""Self-hosted try-on server (catvton-server) reachable over a tunnel.

The "API key" entered in the dashboard is  <base url>|<token>  e.g.
https://abc.trycloudflare.com|3f9c...   (token optional if the server runs without one).
"""
from typing import List, Tuple

import aiohttp

from .base import GenerateRequest, ModelSpec, Output, Provider, ProviderError, ProviderSpec


def parse_key(key: str) -> Tuple[str, str]:
    url, _, token = key.strip().partition("|")
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ProviderError("Local GPU key must be '<https://server-url>|<token>'")
    return url, token.strip()


class LocalProvider(Provider):
    spec = ProviderSpec(
        id="local",
        label="Local GPU (CatVTON)",
        key_label="Server URL|token",
        key_help="Your own try-on server: enter the tunnel URL, a '|', then TRYON_TOKEN from its .env. CatVTON is non-commercial (CC BY-NC-SA).",
        models=[
            ModelSpec("catvton", "CatVTON (garment photo → on model)", ["tryon"],
                      options={"cloth_type": ["upper", "lower", "overall"], "steps": ["30", "40", "50"], "guidance": ["2.5", "3.5"]}),
        ],
    )

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        if req.mode != "tryon":
            raise ProviderError("the local server supports try-on only")
        if not req.model_image or not req.product_images:
            raise ProviderError("try-on needs a model photo and a product image")
        url, token = parse_key(api_key)
        n = int(req.options.get("n", 1))
        outputs: List[Output] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
            for i in range(n):
                form = aiohttp.FormData()
                form.add_field("person", req.model_image.data, filename="person.png", content_type=req.model_image.mime)
                form.add_field("garment", req.product_images[0].data, filename="garment.png", content_type=req.product_images[0].mime)
                form.add_field("cloth_type", req.options.get("cloth_type", "upper"))
                form.add_field("steps", str(req.options.get("steps", "30")))
                form.add_field("guidance", str(req.options.get("guidance", "2.5")))
                form.add_field("seed", str(-1 if n == 1 else 1000 + i))
                try:
                    async with s.post(f"{url}/tryon", data=form, headers={"X-Token": token}) as resp:
                        if resp.status != 200:
                            try:
                                detail = (await resp.json(content_type=None)).get("detail")
                            except Exception:  # noqa: BLE001
                                detail = await resp.text()
                            raise ProviderError(f"local server: HTTP {resp.status} {str(detail)[:300]}")
                        outputs.append(Output(await resp.read(), resp.headers.get("Content-Type", "image/png").split(";")[0]))
                except aiohttp.ClientError as exc:
                    raise ProviderError(f"cannot reach the local server at {url}: {exc}")
        return outputs
