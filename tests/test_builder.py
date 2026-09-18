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
            if "query CloneSource" in q:
                return {"data": {"product": getattr(self, "clone_source", None)}}
            if "query ProductMedia" in q:
                return {"data": {"product": {"id": "P1", "name": "Polo", "media": []}}}
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

        # duplicate SKUs rejected
        bad = dict(body); bad["variants"] = [dict(body["variants"][0]), dict(body["variants"][0])]
        assert c.post("/api/builder/create", headers=H(saleor), json=bad).status_code == 400


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
        r = c.post("/api/studio/pack", headers=H(saleor), json={"product_id": "P1", "product_image_urls": [saleor.base + "/media/a.png"]}).json()
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
