"""Google Gemini native image generation ("Nano Banana" family) via generateContent."""
import base64
from typing import List

import aiohttp

from .base import GenerateRequest, ModelSpec, Output, Provider, ProviderError, ProviderSpec, scene_prompt, tryon_prompt

API = "https://generativelanguage.googleapis.com/v1beta/models"
ASPECTS = ["auto", "1:1", "3:4", "4:3", "9:16", "16:9"]


class GeminiProvider(Provider):
    spec = ProviderSpec(
        id="gemini",
        label="Google Gemini",
        key_label="Gemini API key",
        key_help="aistudio.google.com → Get API key.",
        models=[
            ModelSpec("gemini-3.1-flash-image", "Gemini 3.1 Flash Image", ["scene", "tryon"], options={"aspect_ratio": ASPECTS}),
            ModelSpec("gemini-3-pro-image", "Gemini 3 Pro Image (highest quality)", ["scene", "tryon"], options={"aspect_ratio": ASPECTS}),
            ModelSpec("gemini-2.5-flash-image", "Gemini 2.5 Flash Image", ["scene", "tryon"], options={"aspect_ratio": ASPECTS}),
        ],
    )

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        if req.mode == "tryon":
            if not req.model_image:
                raise ProviderError("a model photo is required for try-on")
            images = [req.model_image, *req.product_images]
            prompt = tryon_prompt(req.prompt)
        else:
            images = list(req.product_images)
            prompt = scene_prompt(req.prompt)
        parts = [{"text": prompt}] + [
            {"inline_data": {"mime_type": img.mime, "data": base64.b64encode(img.data).decode()}} for img in images[:10]
        ]
        config = {"responseModalities": ["IMAGE"]}
        aspect = req.options.get("aspect_ratio", "auto")
        if aspect and aspect != "auto":
            config["imageConfig"] = {"aspectRatio": aspect}
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}

        outputs: List[Output] = []
        n = int(req.options.get("n", 1))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            for _ in range(n):
                async with s.post(f"{API}/{model}:generateContent", json=body,
                                  headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200:
                        raise ProviderError(f"Gemini: {data.get('error', {}).get('message', resp.status)}")
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        inline = part.get("inlineData") or part.get("inline_data")
                        if inline and inline.get("data"):
                            outputs.append(Output(base64.b64decode(inline["data"]), inline.get("mimeType") or inline.get("mime_type") or "image/png"))
                if not outputs:
                    reason = (data.get("candidates") or [{}])[0].get("finishReason") or data.get("promptFeedback", {}).get("blockReason")
                    raise ProviderError(f"Gemini returned no image ({reason or 'unknown reason'})")
        return outputs
