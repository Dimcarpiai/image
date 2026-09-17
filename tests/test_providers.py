"""Provider request building (no network)."""
import pytest

from media_suite.providers import PROVIDERS, GenerateRequest, ImageInput, ProviderError, catalog, find_model
from media_suite.providers.fal_provider import FalProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_catalog_lists_all_modes_and_models():
    c = catalog({"openai": True})
    assert [m["id"] for m in c["modes"]] == ["scene", "tryon", "video"]
    ids = {p["id"]: p for p in c["providers"]}
    assert ids["openai"]["configured"] and not ids["fal"]["configured"]
    assert any("video" in m["modes"] for m in ids["fal"]["models"])
    assert find_model("gemini", "gemini-3.1-flash-image").modes == ["scene", "tryon"]


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
