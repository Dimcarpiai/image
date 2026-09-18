"""Turns Saleor product data + task + locks + presets into the instruction for the image model.
Users never write the base prompt; the free-text field only adds optional extra instructions."""
from typing import Dict, List, Optional

TASKS = {
    "product":   {"label": "Product photo",    "help": "Clean packshot of the product itself.", "mode": "scene"},
    "model":     {"label": "On model",         "help": "The product worn by a person.", "mode": "tryon"},
    "variants":  {"label": "Colour variants",  "help": "Same image in every Saleor colour.", "mode": "edit"},
    "pack":      {"label": "Product pack",     "help": "A set of images in one go.", "mode": "pack"},
    "edit":      {"label": "Edit image",       "help": "Change one thing, keep the rest.", "mode": "edit"},
    "video":     {"label": "Video",            "help": "Short clip from an image.", "mode": "video"},
}

POSES = {
    "standing": "standing straight, facing the camera, arms relaxed",
    "walking": "mid-stride walking towards the camera, natural movement",
    "pockets": "standing with hands in trouser pockets, relaxed shoulders",
    "side": "three-quarter side view, looking slightly away from the camera",
    "sitting": "sitting on a simple stool, relaxed posture, garment fully visible",
    "closeup": "close-up from chest to head, garment details in sharp focus",
}

BACKGROUNDS = {
    "white": "pure white seamless studio background, even soft light, subtle floor shadow",
    "grey": "light grey seamless studio background, soft directional light",
    "beige": "warm beige studio background, soft window light",
    "transparent": "plain white background suitable for cut-out, no props, no shadow",
    "custom": "background, camera angle and lighting exactly like the style reference image",
}

LOCKS = {
    "shape": "the garment's shape, cut, length and proportions",
    "fabric": "the fabric texture and material look",
    "pattern": "the pattern, stripes and print exactly as in the product photo",
    "collar": "the collar type and shape",
    "buttons": "the buttons: same number, placement and style",
    "logo": "the logo exactly as it is: same place, size and design",
    "color": "the exact colour",
}
ALL_LOCKS = list(LOCKS.keys())

LOGO = {
    "preserve": "Keep the existing logo exactly as it is; do not redraw, move or restyle it.",
    "remove": "Remove any logo or brand mark from the garment, leaving the fabric clean and natural.",
    "add": "Place the uploaded company logo (last reference image) on the left chest, small, flat, following the fabric surface; do not alter it.",
    "none": "",
}

EDIT_ACTIONS = {
    "remove_logo": ("Remove the logo from the garment; fill with matching fabric.", ["logo"]),
    "change_color": ("Change the garment colour to {value}; keep everything else identical.", ["color"]),
    "fix_collar": ("Fix the collar so it matches the product photo: correct shape, flat and symmetrical.", []),
    "fix_sleeve": ("Fix the sleeves so they match the product photo: correct length and cuffs.", []),
    "change_trousers": ("Change the trousers to {value}; keep the top garment and the person identical.", []),
    "change_background": ("Change only the background to {value}; keep the person and the garment identical.", []),
    "custom": ("{value}", []),
}

PACKS = {
    "product": {"label": "Product pack", "steps": [
        ("product", {"angle": "front view"}), ("product", {"angle": "back view"}), ("product", {"angle": "side view"}), ("product", {"angle": "fabric detail, macro of the texture and stitching"})]},
    "model": {"label": "Model pack", "steps": [
        ("model", {"pose": "standing"}), ("model", {"pose": "side"}), ("model", {"pose": "walking"}), ("model", {"pose": "closeup"})]},
    "marketing": {"label": "Marketing pack", "steps": [
        ("product", {"background": "white"}), ("product", {"background": "beige", "extra": "lifestyle scene, on a wooden table, morning light"}),
        ("product", {"background": "grey", "extra": "square social-media composition, product centred, generous margins"})]},
}


def product_facts(product: Optional[dict]) -> str:
    if not product:
        return ""
    attrs = product.get("attributes") or {}
    color = attrs.get("color") or attrs.get("colour") or attrs.get("farbe") or ""
    material = attrs.get("material") or attrs.get("fabric") or ""
    gender = attrs.get("gender") or attrs.get("geschlecht") or ""
    bits = [product.get("name", ""), product.get("category") or product.get("product_type") or "", color, material, gender]
    others = [f"{k}: {v}" for k, v in attrs.items() if k not in ("color", "colour", "farbe", "material", "fabric", "gender", "geschlecht") and v]
    return ", ".join(b for b in bits if b) + ("; " + ", ".join(others[:6]) if others else "")


def lock_text(locks: List[str]) -> str:
    keep = [LOCKS[k] for k in locks if k in LOCKS]
    if not keep:
        return ""
    return "Preserve the product exactly — do not change " + "; ".join(keep) + ". The product must look identical to the product reference photo."


def build_prompt(task: str, product: Optional[dict], opts: Dict, refs: Dict[str, int]) -> str:
    """refs: how many images of each type are attached, in order: product, model, fabric, style, logo."""
    facts = product_facts(product)
    locks = opts.get("locks", ALL_LOCKS)
    parts: List[str] = []
    order = [k for k in ("product", "model", "fabric", "style", "logo") if refs.get(k)]
    if order:
        parts.append("Reference images in order: " + ", ".join(f"{refs[k]} {k} reference" + ("s" if refs[k] > 1 else "") for k in order) + ".")
    if task == "product":
        parts.append(f"Photorealistic e-commerce product photo of: {facts}. {opts.get('angle', 'front view')}, product centred and fully visible.")
        parts.append(BACKGROUNDS.get(opts.get("background", "white"), BACKGROUNDS["white"]) + ".")
    elif task == "model":
        parts.append(f"Photorealistic e-commerce photo of the person from the model reference wearing this product: {facts}.")
        parts.append("Keep the person's face, body shape, hairstyle, skin tone and overall appearance identical to the model reference.")
        parts.append("Pose: " + POSES.get(opts.get("pose", "standing"), POSES["standing"]) + ".")
        parts.append(BACKGROUNDS.get(opts.get("background", "white"), BACKGROUNDS["white"]) + ".")
    elif task == "variants":
        parts.append(f"Edit the image: change only the garment colour to {opts.get('color', '')}. Keep the shape, fabric, pattern placement, collar, buttons, logo, the person, the pose, the lighting and the background identical.")
    elif task == "edit":
        template, _ = EDIT_ACTIONS.get(opts.get("action", "custom"), EDIT_ACTIONS["custom"])
        parts.append("Edit the image: " + template.format(value=opts.get("value", "")) + " Change nothing else.")
    elif task == "video":
        parts.append(opts.get("motion") or "slow 360 degree turntable of the product, soft studio light, seamless loop")
    if refs.get("style"):
        parts.append("Match the camera position, framing, crop, background, lighting and visual style of the style reference; replace only the garment.")
    if refs.get("fabric"):
        parts.append("Use the fabric reference for the true texture and weave of the material.")
    if task != "video":
        parts.append(lock_text(locks))
        parts.append(LOGO.get(opts.get("logo", "preserve"), ""))
    if opts.get("extra"):
        parts.append(opts["extra"].strip())
    return " ".join(p for p in parts if p).strip()
