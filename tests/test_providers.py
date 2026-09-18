"""Provider request building (no network)."""
import pytest

from media_suite.providers import PROVIDERS, GenerateRequest, ImageInput, ProviderError, catalog, find_model
from media_suite.providers.fal_provider import FalProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_catalog_lists_all_modes_and_models():
    c = catalog({"openai": True})
    assert [m["id"] for m in c["modes"]] == ["scene", "tryon", "video", "model"]
    ids = {p["id"]: p for p in c["providers"]}
    assert ids["openai"]["configured"] and not ids["fal"]["configured"]
    assert any("video" in m["modes"] for m in ids["fal"]["models"])
    assert find_model("gemini", "gemini-3.1-flash-image").modes == ["scene", "tryon", "model"]


def test_fal_inputs_per_model():
    fal = FalProvider()
    person, garment = ImageInput(PNG), ImageInput(PNG)
    req = GenerateRequest("tryon", "", [garment], person, {"n": 2, "category": "tops"})
    i = fal._input("fal-ai/fashn/tryon/v1.6", req)
    assert i["model_image"].startswith("data:image/png;base64,") and i["category"] == "tops" and i["num_samples"] == 2
    i = fal._input("fal-ai/image-apps-v2/virtual-try-on", req)
    assert set(i) == {"person_image_url", "clothing_image_url"}
    v = fal._input("fal-ai/kling-video/v3/turbo/pro/image-to-video", GenerateRequest("video", "spin", [garment], None, {"duration": "10"}))
    assert v["duration"] == "10" and v["prompt"] == "spin"
    with pytest.raises(ProviderError):
        fal._input("fal-ai/fashn/tryon/v1.6", GenerateRequest("tryon", "", [garment], None))
    with pytest.raises(ProviderError):
        fal._input("fal-ai/fashn/tryon/v1.6", GenerateRequest("scene", "x", [garment], None))


def test_crypto_roundtrip_and_signature():
    from media_suite.crypto import decrypt, encrypt, sign, verify
    assert decrypt(encrypt("sk-abc")) == "sk-abc"
    s = sign("asset1")
    assert verify("asset1", s) and not verify("asset2", s) and not verify("asset1", "1.bad")


def test_stability_forms():
    from io import BytesIO
    from PIL import Image
    from media_suite.providers.stability_provider import StabilityProvider, _fit
    buf = BytesIO(); Image.new("RGB", (900, 600), (1, 2, 3)).save(buf, "PNG"); png = buf.getvalue()
    sp = StabilityProvider()
    src = ImageInput(png)
    path, form = sp._form("relight", GenerateRequest("scene", "on a marble table", [src], None, {"light_source_direction": "left"}))
    fields = {f[0]["name"]: f for f in form._fields}
    assert path.endswith("/replace-background-and-relight") and "subject_image" in fields and fields["light_source_direction"][2] == "left"
    path, form = sp._form("sd3.5-large", GenerateRequest("scene", "beach", [src], None, {"strength": "0.65"}))
    fields = {f[0]["name"]: f[2] for f in form._fields}
    assert path.endswith("/generate/sd3") and fields["mode"] == "image-to-image" and fields["strength"] == "0.65"
    path, form = sp._form("svd", GenerateRequest("video", "", [src], None, {"size": "1024x576"}))
    assert path == "/image-to-video"
    assert Image.open(BytesIO(_fit(png, 1024, 576))).size == (1024, 576)
    with pytest.raises(ProviderError):
        sp._form("relight", GenerateRequest("scene", "x", [], None))
    assert [p["id"] for p in catalog({})["providers"]] == ["openai", "gemini", "stability", "fal", "local"]


def test_stability_tryon_search_replace():
    from media_suite.providers.stability_provider import StabilityProvider
    sp = StabilityProvider()
    person = ImageInput(PNG)
    path, form = sp._form("search-replace", GenerateRequest("tryon", "burgundy rugby shirt", [], person, {"garment": "polo shirt"}))
    fields = {f[0]["name"]: f[2] for f in form._fields}
    assert path.endswith("/search-and-replace") and fields["search_prompt"] == "polo shirt" and "burgundy rugby shirt" in fields["prompt"]
    assert "tryon" in find_model("stability", "search-replace").modes
    with pytest.raises(ProviderError):
        sp._form("search-replace", GenerateRequest("tryon", "x", [], None))


def test_local_provider_key_parsing_and_catalog():
    from media_suite.providers.local_provider import parse_key
    assert parse_key("https://abc.trycloudflare.com/|tok") == ("https://abc.trycloudflare.com", "tok")
    assert parse_key("http://192.168.1.5:8000") == ("http://192.168.1.5:8000", "")
    with pytest.raises(ProviderError):
        parse_key("abc.trycloudflare.com|tok")
    assert [p["id"] for p in catalog({})["providers"]] == ["openai", "gemini", "stability", "fal", "local"]
    assert find_model("local", "catvton").modes == ["tryon"]


async def _fake_server_roundtrip():
    """The provider posts multipart to /tryon and returns the PNG bytes it gets back."""
    import socket, threading, time
    import uvicorn
    from fastapi import FastAPI, File, Form, Header, UploadFile
    from fastapi.responses import Response
    from media_suite.providers.local_provider import LocalProvider
    fake = FastAPI(); seen = {}

    @fake.post("/tryon")
    async def tryon(person: UploadFile = File(...), garment: UploadFile = File(...), cloth_type: str = Form("upper"), x_token: str = Header(default="")):
        seen.update(token=x_token, cloth=cloth_type, person=len(await person.read()), garment=len(await garment.read()))
        return Response(b"\x89PNGfake", media_type="image/png")

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started: time.sleep(0.05)
    out = await LocalProvider().generate("catvton", GenerateRequest("tryon", "", [ImageInput(PNG)], ImageInput(PNG), {"cloth_type": "lower"}), f"http://127.0.0.1:{port}|secret")
    server.should_exit = True
    return out, seen


def test_local_provider_roundtrip():
    import asyncio
    out, seen = asyncio.run(_fake_server_roundtrip())
    assert out[0].data == b"\x89PNGfake" and out[0].mime == "image/png"
    assert seen == {"token": "secret", "cloth": "lower", "person": len(PNG), "garment": len(PNG)}


def test_model_photo_mode_builds_text_only_requests():
    from media_suite.providers.base import model_photo_prompt
    from media_suite.providers.stability_provider import StabilityProvider
    assert "facing the camera" in model_photo_prompt("woman, 30s") and model_photo_prompt("woman, 30s").endswith("woman, 30s")
    path, form = StabilityProvider()._form("core-t2i", GenerateRequest("model", "man, 40s, beard", [], None, {}))
    fields = {f[0]["name"]: f[2] for f in form._fields}
    assert path.endswith("/generate/core") and fields["aspect_ratio"] == "3:4" and "beard" in fields["prompt"]
    assert "model" in find_model("openai", "gpt-image-2.5-sunburst").modes and "model" in find_model("gemini", "gemini-3.1-flash-image").modes



def test_extract_images_from_html():
    from media_suite.scrape import extract_images
    html = """<html><head><meta property="og:image" content="https://i.pinimg.com/736x/ab/cd/main.jpg"></head>
    <body><img src="/static/favicon.png"><img data-src="//cdn.shop.com/images/polo_red.webp?v=2" srcset="a_200.jpg 200w, a_800.jpg 800w">
    <script>{"images":{"orig":{"url":"https:\\/\\/i.pinimg.com\\/originals\\/ab\\/cd\\/big.jpg"}}}</script></body></html>"""
    imgs = extract_images(html, "https://uk.pinterest.com/pin/1/")
    assert imgs[0] == "https://i.pinimg.com/736x/ab/cd/main.jpg"
    assert "https://i.pinimg.com/originals/ab/cd/big.jpg" in imgs
    assert not any("favicon" in u for u in imgs)
    generic = extract_images(html.replace("pinimg.com", "cdn.x.com"), "https://shop.example/p")
    assert "https://cdn.shop.com/images/polo_red.webp?v=2" in generic and "https://shop.example/a_800.jpg" in generic
