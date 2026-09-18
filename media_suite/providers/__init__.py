from typing import Dict, List, Optional

from .base import GenerateRequest, ImageInput, ModelSpec, Output, Provider, ProviderError, ProviderSpec  # noqa: F401
from .fal_provider import FalProvider
from .gemini_provider import GeminiProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider
from .stability_provider import StabilityProvider

PROVIDERS: Dict[str, Provider] = {p.spec.id: p for p in (OpenAIProvider(), GeminiProvider(), StabilityProvider(), FalProvider(), LocalProvider())}

MODES = [
    {"id": "scene", "label": "Scene from prompt", "help": "New photo of the product in a scene you describe."},
    {"id": "tryon", "label": "On a model", "help": "Put the product on a real person from a photo."},
    {"id": "video", "label": "Video", "help": "Short clip animated from a product image."},
    {"id": "model", "label": "AI model photo", "help": "Generate a try-on-ready model photo (front view, plain background) and save it to your model library."},
    {"id": "edit", "label": "Edit image", "help": "Change one thing in an image and keep the rest."},
]


def catalog(configured: Dict[str, bool]) -> dict:
    return {
        "modes": MODES,
        "providers": [
            {
                "id": p.spec.id, "label": p.spec.label, "key_label": p.spec.key_label, "key_help": p.spec.key_help,
                "configured": bool(configured.get(p.spec.id)),
                "models": [{"id": m.id, "label": m.label, "modes": m.modes, "note": m.note, "options": m.options} for m in p.spec.models],
            }
            for p in PROVIDERS.values()
        ],
    }


def find_model(provider_id: str, model_id: str) -> Optional[ModelSpec]:
    p = PROVIDERS.get(provider_id)
    if not p:
        return None
    return next((m for m in p.spec.models if m.id == model_id), None)


# Rough list prices per output (EUR) for the pre-generation estimate and the daily budget guard.
COST_EUR = {
    "openai": 0.06, "gemini": 0.04, "stability": 0.08, "local": 0.0,
    "fal-ai/fashn/tryon/v1.6": 0.07, "fal-ai/image-apps-v2/virtual-try-on": 0.05, "fal-ai/flux-2-lora-gallery/virtual-tryon": 0.06,
    "fal-ai/kling-video/v3/turbo/pro/image-to-video": 0.60, "fal-ai/veo3/image-to-video": 1.20, "bytedance/seedance-2.0/image-to-video": 0.50,
    "stability:svd": 0.20, "stability:core-t2i": 0.03,
}
SECONDS = {"scene": 25, "tryon": 20, "video": 150, "model": 25}


def estimate(provider: str, model: str, mode: str, n: int) -> dict:
    unit = COST_EUR.get(f"{provider}:{model}", COST_EUR.get(model, COST_EUR.get(provider, 0.05)))
    return {"cost_eur": round(unit * n, 2), "seconds": SECONDS.get(mode, 30) * (1 if mode == "video" else 1) + 5 * (n - 1)}
