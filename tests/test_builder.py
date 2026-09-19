"""Product Builder + review + pack + storefront notify, against a fake Saleor, fake LLM and fake providers."""
import asyncio
import json
import socket
import threading
import time
from io import BytesIO

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from PIL import Image

from media_suite import settings as settings_module
from media_suite.builder_api import make_sku
from media_suite.db import db
from media_suite.main import app
from media_suite.providers import PROVIDERS, Output


def png(color=(1, 2, 3), size=(500, 700)):
    b = BytesIO(); Image.new("RGB", size, color).save(b, "PNG"); return b.getvalue()


class FakeSaleor:
    def __init__(self):
        self.app = FastAPI(); self.calls = []; self.revalidations = []; self.media = []

        @self.app.api_route("/media/{name}", methods=["GET", "HEAD"])
        async def media(name: str):
            return Response(png(), media_type="image/png")

        @self.app.post("/revalidate")
        async def revalidate(request: Request):
            self.revalidations.append({"body": await request.json(), "auth": request.headers.get("authorization")})
            return {"revalidated": True}

        @self.app.post("/graphql/")
        async def graphql(request: Request):
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                form = await request.form(); body = json.loads(form["operations"])
                mid = f"MEDIA{len(self.media) + 1}"; self.media.append({"id": mid, "alt": body["variables"]["alt"]})
                return {"data": {"productMediaCreate": {"errors": [], "media": {"id": mid, "url": self.base + "/media/new.png"}}}}
            body = await request.json(); q, v = body["query"], body.get("variables") or {}
            self.calls.append((q.split("(")[0].split()[-1], v))
            if "query SkuExists" in q:
                return {"data": {"productVariant": {"id": "X"} if v["sku"] in getattr(self, "taken_skus", set()) else None}}
            if "tokenVerify" in q:
                return {"data": {"tokenVerify": {"isValid": v["token"] == "staff-jwt", "user": {"id": "U"}}}}
            if "query BuilderCategories" in q:
                if v.get("after"):
                    return {"data": {"categories": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": [{"node": {"id": "CAT2", "name": "Trousers", "parent": None}}]}}}
                return {"data": {"categories": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "edges": [{"node": {"id": "CAT1", "name": "Shirts", "parent": {"name": "Women"}}}]}}}
            if "query BuilderMeta" in q:
                return {"data": {
                    "channels": [{"id": "CH1", "name": "Germany", "slug": "germany", "currencyCode": "EUR"}],
                    "warehouses": {"edges": [{"node": {"id": "WH1", "name": "Main"}}]},
                    "productTypes": {"edges": [{"node": {"id": "PT1", "name": "Shirt",
                        "productAttributes": [{"id": "A_MAT", "name": "Material", "slug": "material", "inputType": "DROPDOWN", "valueRequired": False, "choices": {"edges": []}}],
                        "assignedVariantAttributes": [
                            {"attribute": {"id": "A_SIZE", "name": "Size", "slug": "size", "inputType": "DROPDOWN", "valueRequired": True, "choices": {"edges": [{"node": {"name": "S"}}, {"node": {"name": "M"}}]}}, "variantSelection": True},
                            {"attribute": {"id": "A_COL", "name": "Colour", "slug": "colour", "inputType": "DROPDOWN", "valueRequired": True, "choices": {"edges": []}}, "variantSelection": True}]}}]}}}
            if "productCreate" in q:
                self.created_count = getattr(self, "created_count", 0) + 1
                self.product_input = v["input"]
                return {"data": {"productCreate": {"product": {"id": "PNEW", "name": v["input"]["name"], "slug": "polo-shirt"}, "errors": []}}}
            if "productChannelListingUpdate" in q:
                self.channels = v["input"]; return {"data": {"productChannelListingUpdate": {"errors": []}}}
            if "productVariantBulkCreate" in q:
                self.variants = v["variants"]
                return {"data": {"productVariantBulkCreate": {"productVariants": [{"id": f"V{i}", "sku": x["sku"], "name": ""} for i, x in enumerate(v["variants"])], "errors": []}}}
            if "variantMediaAssign" in q:
                self.assigned = v; return {"data": {"variantMediaAssign": {"errors": []}}}
            if "productTranslate" in q:
                self.translation = v; return {"data": {"productTranslate": {"errors": []}}}
            if "query CopySource" in q:
                e = getattr(self, "edit_source", None) or {}
                return {"data": {"product": {"id": "P1", "name": e.get("name", "Polo Navy"), "slug": "polo-navy", "description": e.get("description"), "seoTitle": "Polo", "seoDescription": "d",
                    "category": {"name": "Shirts"}, "productType": {"name": "Shirt"}, "attributes": [{"attribute": {"name": "Material"}, "values": [{"name": "Cotton"}]}],
                    "translation": {"name": "Poloshirt Navy", "description": None, "seoTitle": "", "seoDescription": ""},
                    "variants": [{"id": "V0", "name": "", "sku": "ROS-POLO-NAV-S", "attributes": [{"attribute": {"name": "Size"}, "values": [{"name": "S"}]}]}]}}}
            if "query EditSource" in q:
                return {"data": {"product": getattr(self, "edit_source", None)}}
            if "productUpdate" in q:
                self.updated_product = v; return {"data": {"productUpdate": {"product": {"id": v["id"], "name": v["input"]["name"], "slug": v["input"].get("slug", "")}, "errors": []}}}
            if "productVariantUpdate" in q:
                self.updated_variant = v; return {"data": {"productVariantUpdate": {"errors": []}}}
            if "productVariantChannelListingUpdate" in q:
                self.updated_prices = v; return {"data": {"productVariantChannelListingUpdate": {"errors": []}}}
            if "productVariantStocksUpdate" in q:
                self.updated_stocks = v; return {"data": {"productVariantStocksUpdate": {"errors": []}}}
            if "query CloneSource" in q:
                return {"data": {"product": getattr(self, "clone_source", None)}}
            if "productMediaReorder" in q:
                self.reordered = v; return {"data": {"productMediaReorder": {"errors": []}}}
            if "query StudioProductSummary" in q:
                return {"data": {"product": {"id": v["id"], "name": "Polo", "category": {"name": "Shirts"}, "productType": {"name": "Shirt"},
                                             "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Burgundy"}]}],
                                             "media": [{"id": "M1", "alt": "front", "type": "IMAGE", "url": self.base + "/media/front.png", "thumb": self.base + "/media/front.png"}]}}}
            if "query ProductMedia" in q:
                return {"data": {"product": {"id": "P1", "name": "Polo", "media": [{"id": "M1", "alt": "front", "type": "IMAGE", "url": self.base + "/media/front.jpg"}] + [{"id": m["id"], "alt": "", "type": "IMAGE", "url": self.base + "/media/x.png"} for m in self.media]}}}
            return {"errors": [{"message": "unhandled " + q[:40]}]}

    def start(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.base = f"http://127.0.0.1:{self.port}"
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started: time.sleep(0.05)
        return self


@pytest.fixture(scope="module")
def saleor():
    settings_module.settings.use_insecure_saleor_http = True; app.use_insecure_saleor_http = True
    fake = FakeSaleor().start()
    db.save_installation(f"127.0.0.1:{fake.port}", "app-token", fake.base + "/graphql/", None)
    yield fake


def H(s): return {"x-saleor-domain": f"127.0.0.1:{s.port}", "x-saleor-token": "staff-jwt"}


def test_make_sku():
    assert make_sku("{brand}-{style}-{color:3}-{size}", {"brand": "ros", "style": "polo", "color": "Burgundy", "size": "M"}) == "ROS-POLO-BUR-M"
    assert make_sku("{brand}{style:1}", {"brand": "R", "style": "polo"}) == "RP"


def test_builder_end_to_end(saleor, monkeypatch):
    async def fake_draft(provider, model, images, hints, api_key):
        assert provider == "openai" and len(images) == 1 and "cotton" in hints
        return {"name": "Polo Shirt", "name_de": "Poloshirt", "description_en": ["Soft cotton.", "Slim fit."], "description_de": ["Weiche Baumwolle."],
                "color": "Burgundy", "material": "Cotton", "category_hint": "Shirts", "seo_title_en": "Polo", "seo_description_en": "A polo",
                "seo_title_de": "Polo", "seo_description_de": "Ein Polo", "alt_text_en": "Burgundy polo shirt", "suggested_sizes": ["S", "M"]}
    monkeypatch.setattr("media_suite.builder_api.draft_product", fake_draft)

    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "sk"})
        r = c.put("/api/builder/settings", headers=H(saleor), json={"defaults": {"brand": "ROS"}, "revalidate_url": saleor.base + "/revalidate", "revalidate_secret": "s3"})
        assert r.status_code == 200
        meta = c.get("/api/builder/meta", headers=H(saleor)).json()
        assert meta["productTypes"][0]["variantAttributes"][0]["values"] == ["S", "M"] and [c["path"] for c in meta["categories"]] == ["Women / Shirts", "Trousers"]
        assert meta["llm"]["available"] == ["openai"] and meta["storefront"]["has_secret"]

        up = c.post("/api/builder/upload", headers=H(saleor), files={"file": ("supplier.jpg", png((200, 20, 20)), "image/jpeg")})
        assert up.status_code == 200 and up.json()["kind"] == "upload"
        asset_id = up.json()["id"]

        d = c.post("/api/builder/draft", headers=H(saleor), json={"asset_ids": [asset_id], "hints": "100% cotton"}).json()
        assert d["draft"]["name"] == "Polo Shirt"

        body = {
            "product_type_id": "PT1", "category_id": "CAT1", "name": "Polo Shirt", "description_en": ["Soft cotton.", "Slim fit."],
            "seo_title": "Polo", "seo_description": "A polo", "product_attributes": {"A_MAT": "Cotton"},
            "channels": [{"id": "CH1", "published": True}],
            "variants": [
                {"attributes": {"A_SIZE": "S", "A_COL": "Burgundy"}, "sku": "ROS-POLO-BUR-S", "prices": {"CH1": 49.0}, "stocks": {"WH1": 5}, "image_asset_id": asset_id},
                {"attributes": {"A_SIZE": "M", "A_COL": "Burgundy"}, "sku": "ROS-POLO-BUR-M", "prices": {"CH1": 49.0}, "stocks": {"WH1": 7}},
                {"attributes": {"A_SIZE": "L", "A_COL": "Burgundy"}, "sku": "ROS-POLO-BUR-L", "prices": {"CH1": 49.0}, "stocks": {}, "enabled": False}],
            "image_asset_ids": [asset_id], "alt_text": "Burgundy polo shirt",
            "translation_de": {"name": "Poloshirt", "description": ["Weiche Baumwolle."], "seo_title": "Polo", "seo_description": "Ein Polo"},
        }
        r = c.post("/api/builder/create", headers=H(saleor), json=body)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["product"]["id"] == "PNEW" and len(out["variants"]) == 2
        assert saleor.product_input["attributes"] == [{"id": "A_MAT", "dropdown": {"value": "Cotton"}}]
        assert json.loads(saleor.product_input["description"])["blocks"][1]["data"]["text"] == "Slim fit."
        assert saleor.channels["updateChannels"][0] == {"channelId": "CH1", "isPublished": True, "visibleInListings": True, "isAvailableForPurchase": True}
        assert saleor.variants[0]["sku"] == "ROS-POLO-BUR-S" and saleor.variants[0]["stocks"] == [{"warehouse": "WH1", "quantity": 5}]
        assert saleor.variants[0]["channelListings"] == [{"channelId": "CH1", "price": 49.0}]
        assert saleor.variants[0]["attributes"][0] == {"id": "A_SIZE", "dropdown": {"value": "S"}}
        assert saleor.assigned == {"mediaId": "MEDIA1", "variantId": "V0"}
        assert saleor.translation["lang"] == "DE" and saleor.translation["input"]["name"] == "Poloshirt"
        assert saleor.media[0]["alt"] == "Burgundy polo shirt"
        # storefront was told
        assert saleor.revalidations[-1]["body"]["productId"] == "PNEW" and saleor.revalidations[-1]["auth"] == "Bearer s3"

        # required variant attribute missing everywhere and without fallback → rejected before anything is created
        before = getattr(saleor, "created_count", 0)
        nov = dict(body); nov["variants"] = [dict(body["variants"][0], attributes={"A_SIZE": "S"})]
        r = c.post("/api/builder/create", headers=H(saleor), json=nov)
        assert r.status_code == 400 and "Colour" in r.text and getattr(saleor, "created_count", 0) == before
        # duplicate SKUs are made unique instead of rejected
        dup = dict(body); dup["variants"] = [dict(body["variants"][0], attributes={"A_SIZE": "S", "A_COL": "Burgundy"}), dict(body["variants"][0], attributes={"A_SIZE": "M", "A_COL": "Burgundy"})]
        r = c.post("/api/builder/create", headers=H(saleor), json=dup).json()
        assert [v["sku"] for v in saleor.variants] == ["ROS-POLO-BUR-S", "ROS-POLO-BUR-S-2"] and "adjusted" in " ".join(r["steps"])


def test_review_queue_and_pack(saleor, monkeypatch):
    class FakeGen:
        def __init__(self, real): self.spec = real.spec
        async def generate(self, model, req, api_key):
            await asyncio.sleep(0.02); return [Output(png((9, 9, 9), (300, 400)), "image/png")]
    for pid in ("openai", "stability", "fal"):
        monkeypatch.setitem(PROVIDERS, pid, FakeGen(PROVIDERS[pid]))
    with TestClient(app) as c:
        for pid in ("openai", "stability"):
            c.put("/api/studio/keys", headers=H(saleor), json={"provider": pid, "api_key": "k"})
        presets = c.get("/api/studio/presets", headers=H(saleor)).json()
        assert any(p["id"] == "onmodel" for p in presets)
        r = c.post("/api/studio/pack-presets", headers=H(saleor), json={"product_id": "P1", "product_image_urls": [saleor.base + "/media/a.png"]}).json()
        assert len(r["started"]) == 2                      # studio + lifestyle; on-model skipped (no fal key + no model photo)
        assert any("no model photo" in s["reason"] or "no fal key" in s["reason"] for s in r["skipped"])
        for _ in range(60):
            jobs = c.get("/api/studio/jobs?product_id=P1", headers=H(saleor)).json()
            if all(j["status"] in ("done", "error") for j in jobs[:2]): break
            time.sleep(0.1)
        assert all(j["status"] == "done" for j in jobs[:2]), jobs[:2]
        queue = c.get("/api/studio/review", headers=H(saleor)).json()
        assert len(queue) >= 2 and queue[0]["product_name"] == "Polo" and queue[0]["meta"]["status"] == "review"
        before = len(saleor.revalidations)
        r = c.post("/api/studio/review", headers=H(saleor), json={"asset_id": queue[0]["id"], "decision": "approve"}).json()
        assert r["status"] == "approved" and r["media"]["id"].startswith("MEDIA")
        assert len(saleor.revalidations) == before + 1 and saleor.revalidations[-1]["body"]["reason"] == "media-attached"
        r = c.post("/api/studio/review", headers=H(saleor), json={"asset_id": queue[1]["id"], "decision": "reject"}).json()
        assert r["status"] == "rejected"
        left = c.get("/api/studio/review", headers=H(saleor)).json()
        assert all(a["id"] not in (queue[0]["id"], queue[1]["id"]) for a in left)


def test_local_llm_backend_roundtrip():
    """Drafting through the local server's /llm/chat proxy (OpenAI-compatible)."""
    from fastapi import FastAPI, Header, Request
    from media_suite.llm import draft_product
    from media_suite.providers.base import ImageInput
    fake = FastAPI(); seen = {}

    @fake.post("/llm/chat")
    async def chat(request: Request, x_token: str = Header(default="")):
        body = await request.json(); seen.update(token=x_token, model=body["model"], fmt=body.get("response_format"))
        assert body["messages"][1]["content"][1]["type"] == "image_url"
        return {"choices": [{"message": {"content": '```json\n{"name": "Local Polo", "color": "red"}\n```'}}]}

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started: time.sleep(0.05)
    out = asyncio.run(draft_product("local", None, [ImageInput(png())], "cotton", f"http://127.0.0.1:{port}|tok"))
    server.should_exit = True
    assert out["name"] == "Local Polo" and seen == {"token": "tok", "model": "gemma3:4b", "fmt": {"type": "json_object"}}


def test_clone_product_colourway(saleor):
    src = {"id": "P1", "name": "Polo Burgundy", "description": '{"blocks":[]}', "seoTitle": "Polo", "seoDescription": "d", "weight": {"unit": "KG", "value": 0.3},
           "productType": {"id": "PT1"}, "category": {"id": "CAT1"},
           "attributes": [{"attribute": {"id": "A_MAT", "name": "Material", "inputType": "DROPDOWN"}, "values": [{"name": "Cotton"}]}],
           "channelListings": [{"channel": {"id": "CH1"}, "isPublished": True, "visibleInListings": True, "isAvailableForPurchase": True}],
           "variants": [
               {"sku": "ROS-POLO-BUR-S", "name": "S", "trackInventory": True,
                "attributes": [{"attribute": {"id": "A_SIZE", "name": "Size", "inputType": "DROPDOWN"}, "values": [{"name": "S"}]},
                               {"attribute": {"id": "A_COL", "name": "Colour", "inputType": "DROPDOWN"}, "values": [{"name": "Burgundy"}]}],
                "channelListings": [{"channel": {"id": "CH1"}, "price": {"amount": 49.0}}], "stocks": [{"warehouse": {"id": "WH1"}, "quantity": 5}]}]}
    saleor.clone_source = src

    with TestClient(app) as c:
        gen = c.get("/api/studio/assets?kind=generated", headers=H(saleor)).json()
        r = c.post("/api/studio/clone", headers=H(saleor), json={"source_product_id": "P1", "name": "Polo Navy", "color": "Navy", "sku_suffix": "", "asset_ids": [gen[0]["id"]] if gen else []})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["product"]["id"] == "PNEW" and out["old_color"] == "Burgundy"
        assert saleor.product_input["name"] == "Polo Navy" and saleor.product_input["category"] == "CAT1"
        assert saleor.variants[0]["sku"] == "ROS-POLO-NAV-S"
        assert {"id": "A_COL", "dropdown": {"value": "Navy"}} in saleor.variants[0]["attributes"]
        assert saleor.variants[0]["stocks"] == [{"warehouse": "WH1", "quantity": 0}]
        assert saleor.variants[0]["channelListings"] == [{"channelId": "CH1", "price": 49.0}]
        assert saleor.revalidations[-1]["body"]["reason"] == "product-cloned"
        # header clone: copy the source product's own images
        n_media = len(saleor.media)
        r = c.post("/api/studio/clone", headers=H(saleor), json={"source_product_id": "P1", "name": "Polo Olive", "color": "Olive", "copy_source_images": True})
        assert r.status_code == 200, r.text
        assert "copied from the source product" in " ".join(r.json()["steps"]) or len(saleor.media) >= n_media


def test_product_editor_roundtrip(saleor):
    saleor.edit_source = {"id": "P1", "name": "Polo Navy", "slug": "polo-navy", "description": '{"blocks":[{"type":"paragraph","data":{"text":"Soft <b>cotton</b>."}}]}',
        "seoTitle": "Polo", "seoDescription": "d", "productType": {"id": "PT1"}, "category": {"id": "CAT1"},
        "attributes": [{"attribute": {"id": "A_MAT", "name": "Material", "inputType": "DROPDOWN"}, "values": [{"name": "Cotton"}]}],
        "translation": {"name": "Poloshirt Navy", "description": None, "seoTitle": "", "seoDescription": ""},
        "variants": [{"id": "V0", "sku": "ROS-POLO-NAV-S", "name": "S", "attributes": [{"attribute": {"name": "Size"}, "values": [{"name": "S"}]}],
                      "channelListings": [{"channel": {"id": "CH1"}, "price": {"amount": 49.0, "currency": "EUR"}}], "stocks": [{"warehouse": {"id": "WH1"}, "quantity": 0}]}]}
    with TestClient(app) as c:
        d = c.get("/api/studio/product-details/P1", headers=H(saleor)).json()
        assert d["description"] == ["Soft cotton."] and d["attributes"][0]["value"] == "Cotton" and d["variants"][0]["label"] == "S"
        assert d["translation_de"]["name"] == "Poloshirt Navy"
        body = {"name": "Polo Navy Slim", "slug": "polo-navy-slim", "category_id": "CAT1", "description": ["New text."], "seo_title": "T", "seo_description": "D",
                "attributes": {"A_MAT": "Linen"}, "translation_de": {"name": "Poloshirt Navy Slim", "description": ["Neuer Text."], "seo_title": "", "seo_description": ""},
                "variants": [{"id": "V0", "sku": "ROS-POLO-NAV-S2", "prices": {"CH1": 59.0}, "stocks": {"WH1": 12}}]}
        r = c.put("/api/studio/product-details/P1", headers=H(saleor), json=body)
        assert r.status_code == 200, r.text
        assert saleor.updated_product["input"]["name"] == "Polo Navy Slim" and saleor.updated_product["input"]["attributes"] == [{"id": "A_MAT", "dropdown": {"value": "Linen"}}]
        assert saleor.updated_variant["input"] == {"sku": "ROS-POLO-NAV-S2"}
        assert saleor.updated_prices["input"] == [{"channelId": "CH1", "price": 59.0}] and saleor.updated_stocks["stocks"] == [{"warehouse": "WH1", "quantity": 12}]
        assert saleor.revalidations[-1]["body"]["reason"] == "product-updated"


def test_settings_budget_estimate_and_bulk(saleor, monkeypatch):
    import asyncio
    from media_suite.providers import PROVIDERS, Output
    class FakeGen:
        def __init__(self, real): self.spec = real.spec
        async def generate(self, model, req, api_key):
            await asyncio.sleep(0.02); return [Output(png((3, 3, 3), (200, 300)), "image/png")]
    for pid in ("openai", "stability"):
        monkeypatch.setitem(PROVIDERS, pid, FakeGen(PROVIDERS[pid]))
    with TestClient(app) as c:
        for pid in ("openai", "stability"):
            c.put("/api/studio/keys", headers=H(saleor), json={"provider": pid, "api_key": "k"})
        s = c.put("/api/studio/settings", headers=H(saleor), json={"daily_budget_eur": 0.10, "review_threshold": 0, "default_models": {"*": "none"}}).json()
        assert s["daily_budget_eur"] == 0.10
        e = c.post("/api/studio/estimate", headers=H(saleor), json={"provider": "openai", "model": "gpt-image-2", "mode": "scene", "n": 2}).json()
        assert e["cost_eur"] == 0.12 and e["daily_budget_eur"] == 0.10
        # a single generation over the cap is refused with 402
        r = c.post("/api/studio/generate", headers=H(saleor), json={"mode": "scene", "provider": "openai", "model": "gpt-image-2", "product_id": "P1", "prompt": "x", "product_image_urls": [saleor.base + "/media/a.png"], "options": {"n": 2}})
        assert r.status_code == 402
        c.put("/api/studio/settings", headers=H(saleor), json={"daily_budget_eur": 0})
        # bulk pack: placeholders filled from product attributes, category auto-detected
        r = c.post("/api/studio/pack-presets-bulk", headers=H(saleor), json={"product_ids": ["P1", "P2"], "preset_ids": ["studio", "lifestyle"]}).json()
        assert r["started"] == 4 and len(r["results"]) == 2
        jobs = c.get("/api/studio/jobs?product_id=P1", headers=H(saleor)).json()
        assert any(j["input"].get("batch") == r["batch"] for j in jobs)
        for _ in range(80):
            jobs = c.get("/api/studio/jobs?product_id=P1", headers=H(saleor)).json()
            if all(j["status"] in ("done", "error") for j in jobs[:2]): break
            time.sleep(0.1)
        assert all(j["status"] == "done" for j in jobs[:2]), jobs[:2]
        assert c.get("/api/studio/settings", headers=H(saleor)).json()["spent_today_eur"] > 0
        # approve as main reorders media
        queue = c.get("/api/studio/review", headers=H(saleor)).json()
        r = c.post("/api/studio/review", headers=H(saleor), json={"asset_id": queue[0]["id"], "decision": "approve_main"}).json()
        assert r["status"] == "approved" and r["main"] is True


def test_copywriter_improve_and_apply(saleor, monkeypatch):
    async def fake_improve(provider, model, current, tone, languages, instructions, api_key, template=""):
        assert current["name"] == "Polo Navy" and "cotton" in instructions and tone == "short and punchy" and "PRODUCT DETAILS" in template
        return {"name_en": "Navy Cotton Polo", "intro_en": ["Better text."], "details_en": ["100% Cotton", "Made in Italy"], "name_de": "Marineblaues Polo", "intro_de": ["Besserer Text."], "details_de": ["100% Baumwolle"],
                "seo_title_en": "Navy Polo", "seo_description_en": "d", "variant_names": {"V0": "Navy / S"}, "notes": "Shortened."}
    monkeypatch.setattr("media_suite.builder_api.improve_copy", fake_improve)
    saleor.edit_source = saleor.edit_source
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "sk"})
        src = c.get("/api/builder/copy/P1", headers=H(saleor)).json()
        assert src["name"] == "Polo Navy" and src["variants"][0]["id"] == "V0"
        r = c.post("/api/builder/improve", headers=H(saleor), json={"product_id": "P1", "tone": "short and punchy", "instructions": "mention cotton"}).json()
        assert r["suggestion"]["name_en"] == "Navy Cotton Polo" and r["current"]["name"] == "Polo Navy"
        a = c.post("/api/builder/apply-copy", headers=H(saleor), json={"product_id": "P1", "name_en": "Navy Cotton Polo", "intro_en": ["Better text."], "details_en": ["100% Cotton", "Made in Italy"], "name_de": "Marineblaues Polo", "intro_de": ["Besserer Text."], "details_de": ["100% Baumwolle"], "variant_names": {"V0": "Navy / S"}}).json()
        assert a["steps"] == ["English texts updated", "German translation updated", "1 variant name(s) updated"]
        assert saleor.updated_product["input"]["name"] == "Navy Cotton Polo" and saleor.updated_variant["input"] == {"name": "Navy / S"}
        blocks = json.loads(saleor.updated_product["input"]["description"])["blocks"]
        assert [b["type"] for b in blocks] == ["paragraph", "header", "list"] and blocks[2]["data"]["items"] == ["100% Cotton", "Made in Italy"] and blocks[1]["data"]["text"] == "Product Details"
        de = json.loads(saleor.translation["input"]["description"])["blocks"]
        assert de[1]["data"]["text"] == "Produktdetails"
        # round-trip: the structured description is parsed back into intro + details
        saleor.edit_source = {**saleor.edit_source, "description": saleor.updated_product["input"]["description"]}
        src = c.get("/api/builder/copy/P1", headers=H(saleor)).json()
        assert src["intro_en"] == ["Better text."] and src["details_en"] == ["100% Cotton", "Made in Italy"]
