"""Text/vision LLM helpers (product drafting, alt text). Uses the OpenAI or Gemini key already saved."""
import base64
import json
import re
from typing import List, Optional

import aiohttp

from .providers.base import ImageInput, ProviderError

DEFAULT_MODELS = {"openai": "gpt-5-mini", "gemini": "gemini-3.1-flash", "local": "gemma3:4b"}

DRAFT_SYSTEM = (
    "You are a merchandiser for an online fashion store. From the product photos and hints, produce product data as JSON. "
    "Be factual: only describe what is visible or given in the hints; never invent brand names, materials or measurements. "
    "Return ONLY a JSON object with keys: name (string, <=70 chars, English), name_de (German), "
    "description_en (2 short paragraphs), description_de (German translation of the same content), "
    "bullets_en (3-5 short strings), bullets_de, color (one word, English), color_de, material (short, or \"\" if unknown), "
    "fit (e.g. regular, slim, relaxed, or \"\"), gender (women|men|unisex|kids), category_hint (e.g. \"Shirts\"), "
    "seo_title_en (<=60 chars), seo_description_en (<=155 chars), seo_title_de, seo_description_de, "
    "alt_text_en (<=120 chars, describes the image for accessibility), alt_text_de, "
    "suggested_sizes (array of size labels typical for this garment type, e.g. [\"XS\",\"S\",\"M\",\"L\",\"XL\"])."
)
ALT_SYSTEM = "Write one concise alt text (<=120 characters) describing this product image for an online shop. Return plain text only."


def _json_from_text(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ProviderError(f"AI returned no JSON: {text[:200]}")
        return json.loads(m.group(0))


async def _openai(model: str, system: str, user_text: str, images: List[ImageInput], api_key: str, json_mode: bool) -> str:
    content = [{"type": "text", "text": user_text}] + [
        {"type": "image_url", "image_url": {"url": img.data_uri(), "detail": "low"}} for img in images[:4]
    ]
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post("https://api.openai.com/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
            data = await _json_or_error(resp, "OpenAI")
            if resp.status != 200:
                raise ProviderError(f"OpenAI: {data.get('error', {}).get('message', resp.status)}")
    return data["choices"][0]["message"]["content"]


async def _gemini(model: str, system: str, user_text: str, images: List[ImageInput], api_key: str, json_mode: bool) -> str:
    parts = [{"text": user_text}] + [{"inline_data": {"mime_type": i.mime, "data": base64.b64encode(i.data).decode()}} for i in images[:4]]
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post(url, json=body, headers={"x-goog-api-key": api_key}) as resp:
            data = await _json_or_error(resp, "Gemini")
            if resp.status != 200:
                raise ProviderError(f"Gemini: {data.get('error', {}).get('message', resp.status)}")
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise ProviderError(f"Gemini returned no text: {str(data)[:200]}")


async def _local(model: str, system: str, user_text: str, images: List[ImageInput], api_key: str, json_mode: bool) -> str:
    """Ollama behind the local try-on server (OpenAI-compatible chat; key is 'url|token')."""
    from .providers.local_provider import parse_key
    url, token = parse_key(api_key)
    content = [{"type": "text", "text": user_text}] + [{"type": "image_url", "image_url": {"url": img.data_uri()}} for img in images[:2]]
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}], "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
        try:
            async with s.post(f"{url}/llm/chat", json=body, headers={"X-Token": token}) as resp:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    raise ProviderError(f"local server at {url} answered HTTP {resp.status} with a non-JSON page — is the tunnel up and the address current?")
                if resp.status != 200:
                    raise ProviderError(f"local LLM: {str(data.get('detail') or data)[:300]}")
        except aiohttp.ClientError as exc:
            raise ProviderError(f"cannot reach the local server at {url}: {exc}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise ProviderError(f"local LLM returned no text: {str(data)[:200]}")


async def _json_or_error(resp: aiohttp.ClientResponse, who: str) -> dict:
    raw = await resp.text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ProviderError(f"{who} answered HTTP {resp.status} with a non-JSON response: {raw[:120]}")


BACKENDS = {"openai": _openai, "gemini": _gemini, "local": _local}


async def draft_product(provider: str, model: Optional[str], images: List[ImageInput], hints: str, api_key: str) -> dict:
    if provider not in BACKENDS:
        raise ProviderError("drafting needs an OpenAI, Gemini or Local GPU key")
    text = await BACKENDS[provider](model or DEFAULT_MODELS[provider], DRAFT_SYSTEM,
                                    f"Hints from the merchandiser: {hints or 'none'}. Produce the JSON now.", images, api_key, True)
    return _json_from_text(text)


async def alt_text(provider: str, model: Optional[str], image: ImageInput, product_name: str, api_key: str) -> str:
    if provider not in BACKENDS:
        return ""
    text = await BACKENDS[provider](model or DEFAULT_MODELS[provider], ALT_SYSTEM, f"Product: {product_name}", [image], api_key, False)
    return text.strip().strip('"')[:120]


QC_SYSTEM = (
    "You are a strict quality checker for fashion catalog images. The first image is the ORIGINAL product photo, the second is an AI-generated image "
    "that must show the SAME product. Compare them and list concrete problems only: changed stripes or pattern, wrong number or placement of buttons, "
    "changed collar, logo missing/changed/invented, wrong colour, changed garment shape or length, deformed hands or body, extra limbs, unreadable or "
    "garbled text, artefacts. Return ONLY JSON: {\"status\": \"ready\"|\"needs_review\", \"issues\": [short strings]} — \"ready\" only when there are no product-accuracy problems."
)


async def qc_check(provider: str, model: Optional[str], reference: Optional[ImageInput], generated: ImageInput, api_key: str) -> dict:
    if provider not in BACKENDS:
        return {"status": "unchecked", "issues": []}
    images = [reference, generated] if reference else [generated]
    text = await BACKENDS[provider](model or DEFAULT_MODELS[provider], QC_SYSTEM,
                                    "Compare and report." if reference else "No original available; check the generated image for AI mistakes only.",
                                    images, api_key, True)
    try:
        out = _json_from_text(text)
        status = "ready" if str(out.get("status", "")).lower().startswith("ready") and not out.get("issues") else "needs_review"
        return {"status": status, "issues": [str(i)[:120] for i in (out.get("issues") or [])][:6]}
    except Exception:  # noqa: BLE001
        return {"status": "unchecked", "issues": [text[:120]]}


DEFAULT_TEMPLATE = """INTRO: 1-2 sentences, editorial tone, naming the garment, its defining material/pattern and the occasion or collection. No prices, no exclamation marks.
PRODUCT DETAILS (bullet list, in this order, only items that are known from the data):
- Colour and material / pattern
- Lining (e.g. Unlined)
- Pockets
- Closure (buttons, zip, hook)
- Cut / fit / leg / sleeve
- Key measurements with the reference size (e.g. Length: 92.5 cm based on a size 38 (IT))
- Made in
- Fabric composition (e.g. 70% Cotton, 30% Polyester)
- Care
Never invent a fact that is not in the data; omit the bullet instead."""

COPY_SYSTEM_TEMPLATE = (
    "You are a senior e-commerce copywriter for a fashion store. You receive the current product data as JSON and a house TEMPLATE. "
    "Rewrite the product copy to follow the template exactly, in the requested languages. Facts must come from the data (attributes, existing text, variants); "
    "never invent materials, measurements, origin or care instructions - omit unknown bullets. Keep brand and product names. No exclamation marks. "
    "Return ONLY a JSON object with keys: name_en, name_de, intro_en (array of 1-2 short paragraphs), intro_de, details_en (array of bullet strings, no leading dashes), "
    "details_de, seo_title_en (<=60 chars), seo_description_en (<=155 chars), seo_title_de, seo_description_de, "
    "variant_names (object: variant id -> short customer-facing name such as \"Navy / M\"), notes (one sentence on what you changed)."
)

COPY_SYSTEM = (
    "You are a senior e-commerce copywriter for a fashion store. You receive the current product data as JSON and must return improved copy as JSON. "
    "Rules: never invent facts (materials, care, origin, measurements) that are not in the input; keep brand and product names; write for the customer, "
    "concrete and specific, no fluff, no exclamation marks; respect the requested tone and languages. Return ONLY a JSON object with keys: "
    "name_en, name_de, description_en (array of 2-3 short paragraphs), description_de (array), bullets_en (3-5 short strings), bullets_de, "
    "seo_title_en (<=60 chars), seo_description_en (<=155 chars), seo_title_de, seo_description_de, "
    "variant_names (object: variant id -> short customer-facing name such as \"Navy / M\"), notes (one sentence on what you changed)."
)


async def improve_copy(provider: str, model: Optional[str], current: dict, tone: str, languages: List[str], instructions: str, api_key: str, template: str = "") -> dict:
    if provider not in BACKENDS:
        raise ProviderError("copywriting needs an OpenAI, Gemini or Local GPU key")
    tpl = (template or DEFAULT_TEMPLATE).strip()
    user = (f"TEMPLATE:\n{tpl}\n\nTone: {tone or 'clear and premium'}. Languages: {', '.join(languages) or 'en, de'}. "
            f"Extra instructions: {instructions or 'none'}.\nCurrent product data:\n{json.dumps(current, ensure_ascii=False)[:12000]}\nReturn the JSON now.")
    out = _json_from_text(await BACKENDS[provider](model or DEFAULT_MODELS[provider], COPY_SYSTEM_TEMPLATE, user, [], api_key, True))
    for lang in ("en", "de"):   # tolerate models that still answer with description_* instead of intro/details
        if f"description_{lang}" in out and f"intro_{lang}" not in out:
            out[f"intro_{lang}"] = out[f"description_{lang}"]
    return out
