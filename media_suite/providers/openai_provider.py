"""OpenAI Images API (gpt-image models) - text+reference-image editing."""
from typing import List

import aiohttp

from .base import GenerateRequest, ImageInput, ModelSpec, Output, Provider, ProviderError, ProviderSpec, model_photo_prompt, scene_prompt, tryon_prompt
import base64

API = "https://api.openai.com/v1"
SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536"]
QUALITIES = ["auto", "low", "medium", "high"]


class OpenAIProvider(Provider):
    spec = ProviderSpec(
        id="openai",
        label="OpenAI",
        key_label="OpenAI API key",
        key_help="platform.openai.com → API keys. Image models may require organization verification.",
        models=[
            ModelSpec("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst (best editing)", ["scene", "tryon", "model"], options={"size": SIZES, "quality": QUALITIES}),
            ModelSpec("gpt-image-2.5-flare", "GPT Image 2.5 Flare (fast)", ["scene", "tryon", "model"], options={"size": SIZES, "quality": QUALITIES}),
            ModelSpec("gpt-image-2", "GPT Image 2", ["scene", "tryon", "model"], options={"size": SIZES, "quality": QUALITIES}),
            ModelSpec("gpt-image-1.5", "GPT Image 1.5", ["scene", "tryon", "model"], options={"size": SIZES, "quality": QUALITIES}),
        ],
    )

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        if req.mode == "model":
            return await self._text_to_image(model, model_photo_prompt(req.prompt), req, api_key)
        images: List[ImageInput] = []
        if req.mode == "tryon":
            if not req.model_image:
                raise ProviderError("a model photo is required for try-on")
            images = [req.model_image, *req.product_images]
            prompt = tryon_prompt(req.prompt)
        else:
            images = list(req.product_images)
            prompt = scene_prompt(req.prompt)
        if not images:
            raise ProviderError("at least one reference image is required")

        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field("prompt", prompt[:32000])
        form.add_field("n", str(int(req.options.get("n", 1))))
        form.add_field("size", req.options.get("size", "auto"))
        form.add_field("quality", req.options.get("quality", "auto"))
        form.add_field("output_format", "png")
        for i, img in enumerate(images[:16]):
            form.add_field("image[]", img.data, filename=f"ref{i}.{img.mime.split('/')[-1]}", content_type=img.mime)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            async with s.post(f"{API}/images/edits", data=form, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise ProviderError(f"OpenAI: {body.get('error', {}).get('message', resp.status)}")
        outputs = []
        for item in body.get("data", []):
            if item.get("b64_json"):
                outputs.append(Output(base64.b64decode(item["b64_json"]), "image/png"))
        if not outputs:
            raise ProviderError("OpenAI returned no image")
        return outputs

    async def _text_to_image(self, model: str, prompt: str, req: GenerateRequest, api_key: str) -> List[Output]:
        body = {"model": model, "prompt": prompt, "n": int(req.options.get("n", 1)),
                "size": req.options.get("size", "1024x1536"), "quality": req.options.get("quality", "auto"), "output_format": "png"}
        if body["size"] == "auto":
            body["size"] = "1024x1536"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            async with s.post(f"{API}/images/generations", json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise ProviderError(f"OpenAI: {data.get('error', {}).get('message', resp.status)}")
        outputs = [Output(base64.b64decode(i["b64_json"]), "image/png") for i in data.get("data", []) if i.get("b64_json")]
        if not outputs:
            raise ProviderError("OpenAI returned no image")
        return outputs
