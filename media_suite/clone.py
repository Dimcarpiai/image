"""Create a new product as a copy of an existing one (new name/colour/SKUs, chosen images)."""
import re
from typing import Dict, List, Optional

from .saleor_api import SaleorAPI, SaleorAPIError


def _attr_input(a: dict, override: Dict[str, str]) -> Optional[dict]:
    attr = a["attribute"]; t = attr["inputType"]
    name_l = attr["name"].lower()
    values = [v.get("name") or v.get("plainText") for v in a["values"] if v.get("name") or v.get("plainText")]
    if "colo" in name_l and override.get("color"):
        values = [override["color"]]
    if not values:
        return None
    if t in ("DROPDOWN", "SWATCH"):
        return {"id": attr["id"], "dropdown": {"value": values[0]}}
    if t == "MULTISELECT":
        return {"id": attr["id"], "multiselect": [{"value": v} for v in values]}
    if t == "PLAIN_TEXT":
        return {"id": attr["id"], "plainText": values[0]}
    return None  # references, files etc. are not cloned


def _sku(old: Optional[str], color: str, suffix: str, replace_from: str) -> Optional[str]:
    if not old:
        return None
    new = old
    if color and replace_from:
        # replace the old colour code (first 3 letters of the old colour) with the new one
        new = re.sub(re.escape(replace_from[:3].upper()), color[:3].upper(), new, count=1, flags=re.I)
    if suffix:
        new = f"{new}-{suffix}"
    return new.upper()


async def clone_product(api: SaleorAPI, source_id: str, name: str, color: str = "", sku_suffix: str = "",
                        images: List[tuple] = (), copy_stock: bool = False, alt: str = "") -> dict:
    src = await api.product_full(source_id)
    if not src:
        raise SaleorAPIError(f"product {source_id} not found")
    override = {"color": color} if color else {}
    old_color = next((v["name"] for a in src["attributes"] if "colo" in a["attribute"]["name"].lower() for v in a["values"] if v.get("name")), "")
    if not old_color:
        old_color = next((v["name"] for var in src["variants"] for a in var["attributes"] if "colo" in a["attribute"]["name"].lower() for v in a["values"] if v.get("name")), "")

    product_input = {
        "productType": src["productType"]["id"], "name": name,
        "description": src.get("description"),
        "seo": {"title": (src.get("seoTitle") or name)[:70], "description": (src.get("seoDescription") or "")[:300]},
        "attributes": [x for x in (_attr_input(a, override) for a in src["attributes"]) if x],
    }
    if src.get("category"):
        product_input["category"] = src["category"]["id"]
    if src.get("weight") and src["weight"].get("value"):
        product_input["weight"] = src["weight"]["value"]
    product = await api.create_product(product_input)
    steps = [f"product created ({product['id']})"]

    if src["channelListings"]:
        await api.update_product_channels(product["id"], [
            {"channelId": c["channel"]["id"], "isPublished": bool(c["isPublished"]), "visibleInListings": bool(c["visibleInListings"]),
             "isAvailableForPurchase": bool(c["isAvailableForPurchase"])} for c in src["channelListings"]])
        steps.append(f"listed in {len(src['channelListings'])} channel(s)")

    variants = []
    for v in src["variants"]:
        variants.append({
            "sku": _sku(v.get("sku"), color, sku_suffix, old_color),
            "attributes": [x for x in (_attr_input(a, override) for a in v["attributes"]) if x],
            "trackInventory": bool(v.get("trackInventory", True)),
            "channelListings": [{"channelId": c["channel"]["id"], "price": c["price"]["amount"]} for c in v["channelListings"] if c.get("price")],
            "stocks": [{"warehouse": s["warehouse"]["id"], "quantity": s["quantity"] if copy_stock else 0} for s in v["stocks"]],
        })
    created = await api.bulk_create_variants(product["id"], variants) if variants else []
    if created:
        steps.append(f"{len(created)} variant(s) copied" + (" with stock" if copy_stock else ", stock set to 0"))

    for i, (data, mime) in enumerate(images):
        await api.create_media(product["id"], data, f"clone-{i}.{mime.split('/')[-1]}", mime, alt)
    if images:
        steps.append(f"{len(images)} image(s) attached")
    return {"product": product, "variants": created, "steps": steps, "old_color": old_color}
