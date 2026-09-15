from io import BytesIO

from PIL import Image

from image_optimizer.optimizer import OptimizeSettings, optimize_image


def make_image(mode="RGB", size=(3000, 2000), fmt="PNG"):
    img = Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30))
    buf = BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def test_resizes_and_converts_to_webp():
    result = optimize_image(make_image(), OptimizeSettings(max_width=1000, max_height=1000))
    assert result.skipped_reason is None
    assert result.format == "webp"
    assert result.new_size == (1000, 667)
    assert result.optimized_bytes < result.original_bytes
    assert Image.open(BytesIO(result.data)).format == "WEBP"


def test_keeps_alpha_for_webp_and_flattens_for_jpeg():
    src = make_image("RGBA", (400, 400))
    webp = optimize_image(src, OptimizeSettings(format="webp"))
    assert Image.open(BytesIO(webp.data)).mode == "RGBA"
    jpeg = optimize_image(src, OptimizeSettings(format="jpeg", skip_if_not_smaller=False))
    assert Image.open(BytesIO(jpeg.data)).mode == "RGB"


def test_keep_format_uses_source_format():
    result = optimize_image(make_image(fmt="JPEG", size=(500, 500)), OptimizeSettings(format="keep", skip_if_not_smaller=False))
    assert result.format == "jpeg"


def test_skips_when_not_smaller():
    tiny = make_image(size=(8, 8))
    result = optimize_image(tiny, OptimizeSettings(format="png"))
    assert result.skipped_reason
    assert result.data == tiny


def test_skips_animated_gif():
    frames = [Image.new("RGB", (20, 20), (i * 80, 0, 0)) for i in range(3)]
    buf = BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    result = optimize_image(buf.getvalue(), OptimizeSettings())
    assert result.skipped_reason == "animated image"
