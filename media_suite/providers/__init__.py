from typing import Dict, List, Optional

from .base import GenerateRequest, ImageInput, ModelSpec, Output, Provider, ProviderError, ProviderSpec  # noqa: F401
from .fal_provider import FalProvider
from .gemini_provider import GeminiProvider
from .openai_provider import OpenAIProvider
from .stability_provider import StabilityProvider

PROVIDERS: Dict[str, Provider] = {p.spec.id: p for p in (OpenAIProvider(), GeminiProvider(), StabilityProvider(), FalProvider())}

MODES = [
    {"id": "scene", "label": "Scene from prompt", "help": "New photo of the product in a scene you describe."},
    {"id": "tryon", "label": "On a model", "help": "Put the product on a real person from a photo."},
    {"id": "video", "label": "Video", "help": "Short clip animated from a product image."},
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
