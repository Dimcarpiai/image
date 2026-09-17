"""Stability AI (London) - Stable Image v2beta: background replace & relight, image-to-image, image-to-video."""
import asyncio
from io import BytesIO
from typing import List

import aiohttp
from PIL import Image

from .base import GenerateRequest, ModelSpec, Output, Provider, ProviderError, ProviderSpec, scene_prompt

API = "https://api.stability.ai/v2beta"
ASPECTS = ["1:1", "3:4", "4:3", "9:16", "16:9"]


class StabilityProvider(Provider):
    spec = ProviderSpec(
        id="stability",
        label="Stability AI",
        key_label="Stability AI API key",
        key_help="platform.stability.ai → API keys. Credits per image; 'Replace background & relight' keeps the product pixels unchanged.",
        models=[
            ModelSpec("relight", "Replace background & relight (product stays identical)", ["scene"],
                      options={"light_source_direction": ["none", "above", "below", "left", "right"], "preserve_original_subject": ["0.6", "0.8", "1.0"]}),
            ModelSpec("ultra", "Stable Image Ultra (image-to-image)", ["scene"],
                      options={"strength": ["0.35", "0.5", "0.65", "0.8"]}),
            ModelSpec("sd3.5-large", "Stable Diffusion 3.5 Large (image-to-image)", ["scene"],
                      options={"strength": ["0.35", "0.5", "0.65", "0.8"]}),
            ModelSpec("svd", "Stable Video Diffusion (image-to-video)", ["video"],
                      options={"motion_bucket_id": ["40", "127", "180"], "size": ["768x768", "1024x576", "576x1024"]}),
        ],
    )

    def _form(self, model: str, req: GenerateRequest) -> tuple:
        """Returns (path, FormData). Kept separate from the network call so it can be tested."""
        if not req.product_images:
            raise ProviderError("select a product image")
        src = req.product_images[0]
        o = req.options
        form = aiohttp.FormData()
        if model == "relight":
            form.add_field("subject_image", src.data, filename="subject.png", content_type=src.mime)
            form.add_field("background_prompt", req.prompt or "clean studio background, soft light")
            form.add_field("preserve_original_subject", str(o.get("preserve_original_subject", "0.6")))
            if o.get("light_source_direction", "none") != "none":
                form.add_field("light_source_direction", o["light_source_direction"])
            form.add_field("output_format", "png")
            return "/stable-image/edit/replace-background-and-relight", form
        if model in ("ultra", "sd3.5-large"):
            form.add_field("image", src.data, filename="ref.png", content_type=src.mime)
            form.add_field("prompt", scene_prompt(req.prompt))
            form.add_field("strength", str(o.get("strength", "0.5")))
            form.add_field("output_format", "png")
            if model == "ultra":
                return "/stable-image/generate/ultra", form
            form.add_field("mode", "image-to-image")
            form.add_field("model", "sd3.5-large")
            return "/stable-image/generate/sd3", form
        if model == "svd":
            w, h = map(int, o.get("size", "768x768").split("x"))
            form.add_field("image", _fit(src.data, w, h), filename="start.png", content_type="image/png")
            form.add_field("motion_bucket_id", str(o.get("motion_bucket_id", "127")))
            form.add_field("cfg_scale", "1.8")
            return "/image-to-video", form
        raise ProviderError(f"unsupported Stability model {model}")

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        outputs: List[Output] = []
        n = int(req.options.get("n", 1))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            for _ in range(n):
                path, form = self._form(model, req)
                headers = {"Authorization": f"Bearer {api_key}", "Accept": "video/*" if model == "svd" else "image/*"}
                if model in ("relight", "svd"):
                    headers["Accept"] = "application/json"
                async with s.post(f"{API}{path}", data=form, headers=headers) as resp:
                    if resp.status not in (200, 202):
                        raise ProviderError(f"Stability: {await _error(resp)}")
                    if model in ("relight", "svd"):
                        gen_id = (await resp.json(content_type=None)).get("id")
                        if not gen_id:
                            raise ProviderError("Stability: no generation id returned")
                        outputs.append(await self._poll(s, model, gen_id, api_key))
                    else:
                        outputs.append(Output(await resp.read(), resp.headers.get("Content-Type", "image/png").split(";")[0]))
        return outputs

    async def _poll(self, s: aiohttp.ClientSession, model: str, gen_id: str, api_key: str) -> Output:
        url = f"{API}/image-to-video/result/{gen_id}" if model == "svd" else f"{API}/results/{gen_id}"
        accept = "video/*" if model == "svd" else "image/*"
        for _ in range(120):
            await asyncio.sleep(5)
            async with s.get(url, headers={"Authorization": f"Bearer {api_key}", "Accept": accept}) as resp:
                if resp.status == 202:
                    continue
                if resp.status != 200:
                    raise ProviderError(f"Stability: {await _error(resp)}")
                mime = resp.headers.get("Content-Type", "").split(";")[0] or ("video/mp4" if model == "svd" else "image/png")
                return Output(await resp.read(), mime)
        raise ProviderError("Stability: generation timed out")


def _fit(data: bytes, w: int, h: int) -> bytes:
    """Stable Video Diffusion needs exact input sizes; letterbox onto white."""
    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((w, h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
        buf = BytesIO(); canvas.save(buf, "PNG"); return buf.getvalue()


async def _error(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
        return "; ".join(body.get("errors") or [body.get("message") or str(body)])
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status}"
