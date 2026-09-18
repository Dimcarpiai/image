"""Common provider interface. Each provider turns a GenerateRequest into a list of output files."""
import base64
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp


class ProviderError(Exception):
    pass


@dataclass
class ImageInput:
    data: bytes
    mime: str = "image/png"

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


@dataclass
class GenerateRequest:
    mode: str                              # scene | tryon | video
    prompt: str
    product_images: List[ImageInput]
    model_image: Optional[ImageInput] = None
    options: Dict = field(default_factory=dict)   # size, quality, n, duration, aspect_ratio ...


@dataclass
class Output:
    data: bytes
    mime: str


@dataclass
class ModelSpec:
    id: str
    label: str
    modes: List[str]
    note: str = ""
    options: Dict[str, List[str]] = field(default_factory=dict)  # option name -> allowed values


@dataclass
class ProviderSpec:
    id: str
    label: str
    key_label: str
    key_help: str
    models: List[ModelSpec]


class Provider:
    spec: ProviderSpec

    async def generate(self, model: str, req: GenerateRequest, api_key: str) -> List[Output]:
        raise NotImplementedError


async def download(url: str, timeout: int = 300) -> Output:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url) as resp:
            if resp.status != 200:
                raise ProviderError(f"download failed: HTTP {resp.status}")
            return Output(await resp.read(), resp.headers.get("Content-Type", "application/octet-stream").split(";")[0])


def tryon_prompt(user_prompt: str) -> str:
    base = (
        "The first image shows a person (the model). The following image(s) show a product. "
        "Create a photorealistic e-commerce photo of this exact person wearing or using this exact product. "
        "Keep the person's face, body, pose, skin tone and the background unchanged. "
        "Reproduce the product faithfully: colors, pattern, print, logo, cut and material."
    )
    return f"{base} {user_prompt}".strip()


def scene_prompt(user_prompt: str) -> str:
    base = (
        "Use the attached product photo(s) as the exact reference. Keep the product identical: shape, colors, "
        "pattern, logo, material and proportions. Generate a new photorealistic e-commerce image: "
    )
    return f"{base}{user_prompt}".strip()


def model_photo_prompt(user_prompt: str) -> str:
    """Prompt tuned to produce inputs that virtual try-on models handle well."""
    base = (
        "Photorealistic e-commerce fashion photo of a single model standing straight, facing the camera, "
        "full upper body visible from head to below the hips, arms relaxed at the sides, neutral expression, "
        "wearing a plain fitted grey t-shirt and plain trousers, no jacket, no accessories, no logos, "
        "plain light grey studio background, even soft lighting, sharp focus, portrait orientation, 3:4. "
    )
    return f"{base}{user_prompt}".strip()
