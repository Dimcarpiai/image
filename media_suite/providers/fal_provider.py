"""fal.ai queue API: virtual try-on models and image-to-video models."""
import asyncio
from typing import List

import aiohttp

from .base import GenerateRequest, ModelSpec, Output, Provider, ProviderError, ProviderSpec, download

QUEUE = "https://queue.fal.run"
DURATIONS = ["5", "10"]


class FalProvider(Provider):
    spec = ProviderSpec(
        id="fal",
        label="fal.ai",
        key_label="fal.ai API key (FAL_KEY)",
        key_help="fal.ai/dashboard/keys. Pay per image / per second of video.",
        models=[
            ModelSpec("fal-ai/fashn/tryon/v1.6", "FASHN Try-On v1.6 (prints & logos accurate)", ["tryon"],
                      options={"category": ["auto", "tops", "bottoms", "one-pieces"]}),
            ModelSpec("fal-ai/image-apps-v2/virtual-try-on", "Image Apps v2 Try-On", ["tryon"]),
            ModelSpec("fal-ai/flux-2-lora-gallery/virtual-tryon", "FLUX 2 Try-On (prompt-styled)", ["tryon"]),
            ModelSpec("fal-ai/kling-video/v3/turbo/pro/image-to-video", "Kling 3.0 Turbo Pro (1080p)", ["video"], options={"duration": DURATIONS}),
            ModelSpec("fal-ai/veo3/image-to-video", "Google Veo 3", ["video"], options={"aspect_ratio": ["16:9", "9:16"]}),
            ModelSpec("bytedance/seedance-2.0/image-to-video", "Seedance 2.0", ["video"], options={"resolution": ["720p", "1080p"]}),
        ],
    )

    def _input(self, model: str, req: GenerateRequest) -> dict:
        if req.mode == "tryon":
            if not req.model_image or not req.product_images:
                raise ProviderError("try-on needs a model photo and a product image")
            person, garment = req.model_image.data_uri(), req.product_images[0].data_uri()
            if model.startswith("fal-ai/fashn"):
                inp = {"model_image": person, "garment_image": garment, "num_samples": int(req.options.get("n", 1))}
                cat = req.options.get("category", "auto")
                if cat != "auto":
                    inp["category"] = cat
                return inp
            if model.startswith("fal-ai/image-apps-v2"):
                return {"person_image_url": person, "clothing_image_url": garment}
            if model.startswith("fal-ai/flux-2-lora-gallery"):
                return {"image_urls": [person, garment], "prompt": req.prompt or "A person wearing the garment, virtual try-on",
                        "num_images": int(req.options.get("n", 1))}
            raise ProviderError(f"unsupported try-on model {model}")
        if req.mode == "video":
            if not req.product_images:
                raise ProviderError("video needs a start image")
            inp = {"image_url": req.product_images[0].data_uri(), "prompt": req.prompt or "Slow cinematic camera move around the product, studio lighting."}
            if "kling-video" in model:
                inp["duration"] = str(req.options.get("duration", "5"))
            if "veo3" in model:
                inp["aspect_ratio"] = req.options.get("aspect_ratio", "16:9")
            if "seedance" in model:
                inp["resolution"] = req.options.get("resolution", "720p")
            return inp
        raise ProviderError("fal.ai models in this app support try-on and video only")

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.post(f"{QUEUE}/{model}", json=self._input(model, req), headers=headers) as resp:
                sub = await resp.json(content_type=None)
                if resp.status not in (200, 201, 202):
                    raise ProviderError(f"fal.ai: {sub.get('detail') or sub}")
            status_url, response_url = sub["status_url"], sub["response_url"]
            deadline = asyncio.get_event_loop().time() + 900
            while True:
                await asyncio.sleep(3)
                async with s.get(status_url, headers=headers) as resp:
                    st = await resp.json(content_type=None)
                if st.get("status") == "COMPLETED":
                    break
                if st.get("status") in ("FAILED", "ERROR"):
                    raise ProviderError(f"fal.ai job failed: {st}")
                if asyncio.get_event_loop().time() > deadline:
                    raise ProviderError("fal.ai job timed out")
            async with s.get(response_url, headers=headers) as resp:
                result = await resp.json(content_type=None)
                if resp.status != 200:
                    raise ProviderError(f"fal.ai: {result.get('detail') or result}")

        urls: List[str] = []
        if isinstance(result.get("images"), list):
            urls = [i["url"] for i in result["images"] if isinstance(i, dict) and i.get("url")]
        elif isinstance(result.get("image"), dict) and result["image"].get("url"):
            urls = [result["image"]["url"]]
        elif isinstance(result.get("video"), dict) and result["video"].get("url"):
            urls = [result["video"]["url"]]
        if not urls:
            raise ProviderError(f"fal.ai returned no media: {str(result)[:200]}")
        outputs = [await download(u) for u in urls]
        for o in outputs:
            if o.mime == "application/octet-stream":
                o.mime = "video/mp4" if req.mode == "video" else "image/png"
        return outputs
