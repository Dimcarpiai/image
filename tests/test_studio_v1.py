"""Task-oriented studio: prompt building, provider auto-pick, variants, packs, publish, model lock, versions, bulk."""
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
from media_suite.db import db
from media_suite.main import app
from media_suite.prompting import ALL_LOCKS, build_prompt
from media_suite.providers import PROVIDERS, Output


def png(color=(1, 2, 3), size=(300, 400)):
    b = BytesIO(); Image.new("RGB", size, color).save(b, "PNG"); return b.getvalue()


class FakeSaleor:
    def __init__(self):
        self.app = FastAPI(); self.media = []; self.assigned = []; self.reordered = None

        @self.app.api_route("/media/{name}", methods=["GET", "HEAD"])
        async def media(name: str):
            return Response(png(), media_type="image/png")

        @self.app.post("/graphql/")
        async def graphql(request: Request):
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                form = await request.form(); mid = f"MEDIA{len(self.media) + 1}"; self.media.append({"id": mid})
                return {"data": {"productMediaCreate": {"errors": [], "media": {"id": mid, "url": self.base + "/media/new.png"}}}}
            body = await request.json(); q, v = body["query"], body.get("variables") or {}
            if "query SkuExists" in q:
                return {"data": {"productVariant": {"id": "X"} if v["sku"] in getattr(self, "taken_skus", set()) else None}}
            if "tokenVerify" in q:
                return {"data": {"tokenVerify": {"isValid": v["token"] == "staff-jwt", "user": {"id": "U"}}}}
            if "query StudioProductSummary" in q:
                return {"data": {"product": {"id": v["id"], "name": "Rugby Shirt", "category": {"name": "Shirts"}, "productType": {"name": "Shirt"},
                    "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Burgundy"}]}, {"attribute": {"name": "Material", "slug": "material"}, "values": [{"name": "Cotton"}]}],
                    "media": [{"id": "M1", "alt": "", "type": "IMAGE", "url": self.base + "/media/front.png", "thumb": self.base + "/media/front.png"}]}}}
            if "query VariantSetup" in q:
                return {"data": {"product": {"id": v["id"], "name": "Rugby Shirt",
                    "productType": {"id": "PT1", "name": "Shirt", "assignedVariantAttributes": [
                        {"attribute": {"id": "A_SIZE", "name": "Size", "slug": "size", "inputType": "DROPDOWN", "valueRequired": True, "choices": {"edges": [{"node": {"name": "S"}}, {"node": {"name": "M"}}]}}},
                        {"attribute": {"id": "A_COL", "name": "Colour", "slug": "color", "inputType": "DROPDOWN", "valueRequired": False, "choices": {"edges": []}}},
                        {"attribute": {"id": "A_FIT", "name": "Fit", "slug": "fit", "inputType": "DROPDOWN", "valueRequired": True, "choices": {"edges": [{"node": {"name": "Regular"}}]}}}]},
                    "channelListings": [{"channel": {"id": "CH1", "name": "Germany", "currencyCode": "EUR"}}],
                    "variants": [{"id": "V1", "sku": "R-BUR-S", "attributes": [{"attribute": {"id": "A_SIZE", "slug": "size", "name": "Size"}, "values": [{"name": "S"}]}, {"attribute": {"id": "A_COL", "slug": "color", "name": "Colour"}, "values": [{"name": "Burgundy"}]}, {"attribute": {"id": "A_FIT", "slug": "fit", "name": "Fit"}, "values": [{"name": "Slim"}]}],
                                  "channelListings": [{"channel": {"id": "CH1"}, "price": {"amount": 49.0}}], "stocks": [{"warehouse": {"id": "WH1"}, "quantity": 5}]}]}}}
            if "query BuilderMeta" in q:
                return {"data": {"channels": [{"id": "CH1", "name": "Germany", "slug": "germany", "currencyCode": "EUR"}], "warehouses": {"edges": [{"node": {"id": "WH1", "name": "Main"}}]}, "productTypes": {"edges": []}}}
            if "query BuilderCategories" in q:
                return {"data": {"categories": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": []}}}
            if "productVariantBulkCreate" in q:
                self.bulk_variants = v["variants"]
                return {"data": {"productVariantBulkCreate": {"productVariants": [{"id": f"NV{i}", "sku": x["sku"], "name": ""} for i, x in enumerate(v["variants"])], "errors": []}}}
            if "query SkuVariants" in q:
                return {"data": {"product": {"id": v["id"], "name": "Rugby Shirt", "slug": "rugby-shirt", "category": {"name": "Shirts"},
                    "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Burgundy"}]}],
                    "variants": [{"id": "V1", "sku": "R-BUR-S", "name": "S", "attributes": [{"attribute": {"name": "Size", "slug": "size"}, "values": [{"name": "S"}]}]},
                                 {"id": "V2", "sku": "R-NAV-S", "name": "S", "attributes": [{"attribute": {"name": "Size", "slug": "size"}, "values": [{"name": "S"}]}, {"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Navy"}]}]},
                                 {"id": "V3", "sku": "", "name": "", "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Green"}]}]}]}}}
            if "query StudioVariants" in q:
                return {"data": {"product": {"id": v["id"], "name": "Rugby Shirt", "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Burgundy"}]}],
                    "variants": [{"id": "V1", "sku": "R-BUR-S", "name": "S", "attributes": [{"attribute": {"name": "Size", "slug": "size"}, "values": [{"name": "S"}]}, {"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Burgundy"}]}], "media": []},
                                 {"id": "V2", "sku": "R-NAV-S", "name": "S", "attributes": [{"attribute": {"name": "Size", "slug": "size"}, "values": [{"name": "S"}]}, {"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Navy"}]}], "media": []},
                                 {"id": "V3", "sku": "R-GRN-S", "name": "S", "attributes": [{"attribute": {"name": "Colour", "slug": "color"}, "values": [{"name": "Green"}]}], "media": []}],
                    "media": []}}}
            if "query ProductMedia" in q:
                return {"data": {"product": {"id": "P1", "name": "Rugby Shirt", "media": [{"id": "M1", "alt": "", "type": "IMAGE", "url": self.base + "/media/front.png"}] + [{"id": m["id"], "alt": "", "type": "IMAGE", "url": self.base + "/media/x.png"} for m in self.media]}}}
            if "variantMediaAssign" in q:
                self.assigned.append(v); return {"data": {"variantMediaAssign": {"errors": []}}}
            if "productMediaReorder" in q:
                self.reordered = v; return {"data": {"productMediaReorder": {"errors": []}}}
            return {"errors": [{"message": "unhandled " + q[:40]}]}

    def start(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.base = f"http://127.0.0.1:{self.port}"
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started: time.sleep(0.05)
        return self


class Recorder:
    def __init__(self, real): self.spec = real.spec; self.calls = []
    async def generate(self, model, req, api_key):
        self.calls.append({"model": model, "mode": req.mode, "prompt": req.prompt, "n_images": len(req.product_images), "has_model": req.model_image is not None, "options": req.options})
        await asyncio.sleep(0.02); return [Output(png((7, 7, 7)), "image/png")]


@pytest.fixture(scope="module")
def saleor():
    settings_module.settings.use_insecure_saleor_http = True; app.use_insecure_saleor_http = True
    fake = FakeSaleor().start()
    db.save_installation(f"127.0.0.1:{fake.port}", "app-token", fake.base + "/graphql/", None)
    yield fake


def H(s): return {"x-saleor-domain": f"127.0.0.1:{s.port}", "x-saleor-token": "staff-jwt"}


def wait_done(c, s, product_id="P1"):
    for _ in range(100):
        res = c.get(f"/api/studio/results?product_id={product_id}", headers=H(s)).json()
        if res and all(r["status"] in ("done", "error") for r in res): return res
        time.sleep(0.1)
    raise AssertionError(res)


def test_prompt_builder_locks_and_presets():
    p = {"name": "Rugby Shirt", "category": "Shirts", "attributes": {"color": "Burgundy", "material": "Cotton"}}
    t = build_prompt("model", p, {"pose": "pockets", "background": "grey", "logo": "remove", "locks": ALL_LOCKS, "extra": "smiling"}, {"product": 1, "model": 1})
    assert "hands in trouser pockets" in t and "light grey" in t and "Remove any logo" in t and "the buttons" in t and t.endswith("smiling")
    t = build_prompt("variants", p, {"color": "Navy", "locks": [l for l in ALL_LOCKS if l != "color"]}, {"product": 1})
    assert "change only the garment colour to Navy" in t and "the exact colour" not in t
    t = build_prompt("edit", p, {"action": "fix_collar", "locks": []}, {"product": 1})
    assert "Fix the collar" in t and "Change nothing else" in t


def test_run_model_task_auto_provider_and_publish(saleor, monkeypatch):
    rec = Recorder(PROVIDERS["openai"]); monkeypatch.setitem(PROVIDERS, "openai", rec)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "k"})
        t = c.get("/api/studio/tasks", headers=H(saleor)).json()
        assert [x["id"] for x in t["tasks"]][:3] == ["product", "model", "variants"] and t["providers_ready"]["tryon"] == ["openai", "gpt-image-2.5-sunburst"]
        # model photo needed for the model task
        m = c.post("/api/studio/assets/models", headers=H(saleor), files={"file": ("m.png", png((9, 9, 9)), "image/png")}, data={"label": "Anna"}).json()
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_id": m["id"]},
                                                              "options": {"pose": "walking", "background": "beige", "logo": "preserve"}})
        assert r.status_code == 200, r.text
        assert r.json()["provider"] == "openai"
        res = wait_done(c, saleor)
        assert res[0]["status"] == "done" and res[0]["task"] == "model"
        call = rec.calls[-1]
        assert call["mode"] == "tryon" and call["has_model"] and call["options"]["raw_prompt"] is True and "walking" in call["prompt"] and "beige" in call["prompt"]
        asset = res[0]["assets"][0]
        assert asset["meta"]["version"] == 1 and asset["meta"]["task"] == "model"
        # publish: thumbnail
        p = c.post("/api/studio/publish", headers=H(saleor), json={"asset_id": asset["id"], "action": "thumbnail"}).json()
        assert p["status"] == "published" and saleor.reordered["mediaIds"][0] == p["media"]["id"]
        # keep this model
        lk = c.post("/api/studio/model-lock", headers=H(saleor), json={"asset_id": asset["id"], "label": "House model"}).json()
        assert lk["asset"]["kind"] == "model" and c.get("/api/studio/tasks", headers=H(saleor)).json()["locked_model"] == lk["locked_model"]
        # next model task without an explicit model uses the locked one
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"]}})
        assert r.status_code == 200 and r.json()["input"]["model_asset_id"] == lk["locked_model"]
        wait_done(c, saleor)


def test_variants_generator_and_versions_and_add_variant(saleor, monkeypatch):
    rec = Recorder(PROVIDERS["openai"]); monkeypatch.setitem(PROVIDERS, "openai", rec)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "k"})
        vi = c.get("/api/studio/variants/P1", headers=H(saleor)).json()
        assert vi["product_color"] == "Burgundy" and vi["colors"] == ["Burgundy", "Green", "Navy"]
        source = [a for r in c.get("/api/studio/results?product_id=P1", headers=H(saleor)).json() for a in r["assets"]][0]
        r = c.post("/api/studio/variants/generate", headers=H(saleor), json={"product_id": "P1", "source_asset_id": source["id"]})
        assert r.status_code == 200, r.text
        jobs = r.json()["jobs"]
        assert sorted(j["input"]["options"] and j["input"]["variant_id"] for j in jobs) == ["V2", "V3"]     # Navy, Green; Burgundy skipped
        res = wait_done(c, saleor)
        navy = next(r for r in res if r["variant_id"] == "V2")
        assert navy["status"] == "done" and "Navy" in rec.calls[-1]["prompt"] or "Navy" in rec.calls[-2]["prompt"]
        a = navy["assets"][0]
        assert a["meta"]["parent_id"] == source["id"] and a["meta"]["version"] == 2 and a["meta"]["variant_id"] == "V2"
        chain = c.get(f"/api/studio/versions/{a['id']}", headers=H(saleor)).json()
        assert chain[0]["id"] == source["id"] and any(x["current"] for x in chain) and len(chain) >= 3
        # add to variant (uses the remembered variant id)
        p = c.post("/api/studio/publish", headers=H(saleor), json={"asset_id": a["id"], "action": "add_variant"}).json()
        assert p["status"] == "published" and saleor.assigned[-1]["variantId"] == "V2"


def test_pack_and_bulk_with_look(saleor, monkeypatch):
    rec = Recorder(PROVIDERS["openai"]); monkeypatch.setitem(PROVIDERS, "openai", rec)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "k"})
        look = c.put("/api/studio/looks", headers=H(saleor), json={"name": "Winter Polo", "background": "grey", "pose": "side"}).json()
        r = c.post("/api/studio/pack", headers=H(saleor), json={"product_id": "P1", "pack": "product", "look_id": look["id"]})
        assert r.status_code == 200 and len(r.json()["jobs"]) == 4
        r = c.post("/api/studio/bulk", headers=H(saleor), json={"product_ids": ["P1", "P2"], "task": "product", "look_id": look["id"]}).json()
        assert r["started"] == 2 and all("jobs" in x for x in r["results"])
        wait_done(c, saleor); wait_done(c, saleor, "P2")
        assert any("light grey" in call["prompt"] for call in rec.calls)
        assert c.delete(f"/api/studio/looks/{look['id']}", headers=H(saleor)).json()["deleted"] == look["id"]


def test_pose_aware_provider_and_background_actions(saleor, monkeypatch):
    class FalRec(Recorder): pass
    fal = FalRec(PROVIDERS["fal"]); oai = Recorder(PROVIDERS["openai"]); stab = Recorder(PROVIDERS["stability"])
    monkeypatch.setitem(PROVIDERS, "fal", fal); monkeypatch.setitem(PROVIDERS, "openai", oai); monkeypatch.setitem(PROVIDERS, "stability", stab)
    with TestClient(app) as c:
        for pid in ("fal", "openai", "stability"):
            c.put("/api/studio/keys", headers=H(saleor), json={"provider": pid, "api_key": "k"})
        m = c.post("/api/studio/assets/models", headers=H(saleor), files={"file": ("m.png", png(), "image/png")}, data={"label": "M"}).json()
        # standing pose -> FASHN (image-only engine); walking -> prompt-aware engine
        r1 = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_id": m["id"]}, "options": {"pose": "standing"}}).json()
        r2 = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_id": m["id"]}, "options": {"pose": "walking"}}).json()
        assert r1["provider"] == "fal" and r2["provider"] == "openai"
        res = wait_done(c, saleor)
        src = next(a for r in res for a in r["assets"])
        r3 = c.post("/api/studio/run", headers=H(saleor), json={"task": "remove_bg", "product_id": "P1", "refs": {"source_asset_id": src["id"]}}).json()
        r4 = c.post("/api/studio/run", headers=H(saleor), json={"task": "replace_bg", "product_id": "P1", "refs": {"source_asset_id": src["id"]}, "options": {"background": "beige"}}).json()
        assert (r3["provider"], r3["model"]) == ("stability", "remove-bg") and (r4["provider"], r4["model"]) == ("stability", "relight")
        wait_done(c, saleor)
        assert any(call["options"].get("background_prompt", "").startswith("warm beige") for call in stab.calls)


def test_sku_generation_and_qr(saleor):
    from PIL import Image as _I
    with TestClient(app) as c:
        c.put("/api/builder/settings", headers=H(saleor), json={"defaults": {"sku_pattern": "{brand}-{style}-{color:3}-{size}", "brand": "ROS"}})
        r = c.post("/api/studio/skus", headers=H(saleor), json={"product_id": "P1", "apply": False}).json()
        assert [x["sku"] for x in r["rows"]] == ["ROS-RUGB-BUR-S", "ROS-RUGB-NAV-S", "ROS-RUGB-GRE"]
        assert r["applied"] == 0
        c.put("/api/studio/storefront-url", headers=H(saleor), json={"product_url": "https://rostau.de/p/{slug}?variant={sku}"})
        q = c.get("/api/studio/qr/product/P1", headers=H(saleor)).json()
        assert q["items"][0]["url"] == "https://rostau.de/p/rugby-shirt" and q["items"][1]["url"] == "https://rostau.de/p/rugby-shirt?variant=R-BUR-S"
        png_resp = c.get("/api/studio/qr", headers=H(saleor), params={"url": q["items"][1]["url"], "label": "R-BUR-S"})
        assert png_resp.status_code == 200 and _I.open(BytesIO(png_resp.content)).size == (512, 556)


def test_explicit_provider_without_model(saleor, monkeypatch):
    stab = Recorder(PROVIDERS["stability"]); monkeypatch.setitem(PROVIDERS, "stability", stab)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "stability", "api_key": "k"})
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "product", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"]}, "advanced": {"provider": "stability"}}).json()
        assert (r["provider"], r["model"]) == ("stability", "relight")
        wait_done(c, saleor)


def test_model_parameters_pass_through(saleor, monkeypatch):
    loc = Recorder(PROVIDERS["local"]); monkeypatch.setitem(PROVIDERS, "local", loc)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "local", "api_key": "http://127.0.0.1:1|t"})
        m = c.post("/api/studio/assets/models", headers=H(saleor), files={"file": ("m.png", png(), "image/png")}, data={"label": "M"}).json()
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_id": m["id"]},
                                                              "advanced": {"provider": "local", "model": "catvton", "options": {"cloth_type": "overall", "steps": "50", "bogus": "x"}}}).json()
        assert r["provider"] == "local"
        wait_done(c, saleor)
        o = loc.calls[-1]["options"]
        assert o["cloth_type"] == "overall" and o["steps"] == "50" and "bogus" not in o


def test_preferred_provider_setting(saleor, monkeypatch):
    loc = Recorder(PROVIDERS["local"]); oai = Recorder(PROVIDERS["openai"]); monkeypatch.setitem(PROVIDERS, "local", loc); monkeypatch.setitem(PROVIDERS, "openai", oai)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "local", "api_key": "http://127.0.0.1:1|t"})
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "k"})
        c.put("/api/studio/settings", headers=H(saleor), json={"preferred_providers": {"tryon": "local"}})
        m = c.post("/api/studio/assets/models", headers=H(saleor), files={"file": ("m.png", png(), "image/png")}, data={"label": "M"}).json()
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_id": m["id"]}, "options": {"pose": "walking"}}).json()
        assert r["provider"] == "local"          # preference wins even for a pose that would otherwise route to a prompt engine
        wait_done(c, saleor)
        c.put("/api/studio/settings", headers=H(saleor), json={"preferred_providers": {"tryon": ""}})


def test_add_variants_colour_x_size(saleor):
    with TestClient(app) as c:
        c.put("/api/builder/settings", headers=H(saleor), json={"defaults": {"sku_pattern": "{brand}-{style}-{color:3}-{size}", "brand": "ROS"}})
        st = c.get("/api/studio/variant-setup/P1", headers=H(saleor)).json()
        assert st["product_type"] == "Shirt" and [a["name"] for a in st["all_attributes"]] == ["Size", "Colour", "Fit"]
        assert st["attributes"]["color"]["id"] == "A_COL" and st["attributes"]["size"]["values"] == ["S", "M"] and st["used_colors"] == ["Burgundy"]
        assert st["existing"][0]["values"] == {"A_SIZE": "S", "A_COL": "Burgundy", "A_FIT": "Slim"}
        saleor.taken_skus = {"ROS-RUGB-NAV-S"}                  # already used by another product in the shop
        r = c.post("/api/studio/variants/create", headers=H(saleor), json={"product_id": "P1", "colors": ["Burgundy", "Navy"], "sizes": ["S", "M"], "price": 49.0, "stock": 3}).json()
        assert r["count"] == 3                                   # Burgundy/S exists → skipped
        skus = sorted(v["sku"] for v in saleor.bulk_variants)
        assert skus == ["ROS-RUGB-BUR-M", "ROS-RUGB-NAV-M", "ROS-RUGB-NAV-S-2"]
        saleor.taken_skus = set()
        v = saleor.bulk_variants[0]
        assert v["channelListings"] == [{"channelId": "CH1", "price": 49.0}] and v["stocks"] == [{"warehouse": "WH1", "quantity": 3}]
        assert {"id": "A_FIT", "dropdown": {"value": "Slim"}} in v["attributes"]          # required extra attribute copied from an existing variant
        bad = c.post("/api/studio/variants/create", headers=H(saleor), json={"product_id": "P1", "colors": ["Green"], "sizes": []})
        assert bad.status_code == 400 and "Size" in bad.text
        # generic form: any attribute can be an axis (Fit x Size), colour copied from the existing variant
        r = c.post("/api/studio/variants/create", headers=H(saleor), json={"product_id": "P1", "values": {"A_FIT": ["Regular"], "A_SIZE": ["S", "M"]}}).json()
        assert r["count"] == 2 and all({"id": "A_COL", "dropdown": {"value": "Burgundy"}} in v["attributes"] for v in saleor.bulk_variants)


def test_multiple_model_photos_make_one_job_each(saleor, monkeypatch):
    rec = Recorder(PROVIDERS["openai"]); monkeypatch.setitem(PROVIDERS, "openai", rec)
    with TestClient(app) as c:
        c.put("/api/studio/keys", headers=H(saleor), json={"provider": "openai", "api_key": "k"})
        c.put("/api/studio/settings", headers=H(saleor), json={"preferred_providers": {"tryon": "openai"}})
        ids = [c.post("/api/studio/assets/models", headers=H(saleor), files={"file": (f"m{i}.png", png((i, i, i)), "image/png")}, data={"label": f"M{i}"}).json()["id"] for i in range(3)]
        r = c.post("/api/studio/run", headers=H(saleor), json={"task": "model", "product_id": "P1", "refs": {"product_urls": [saleor.base + "/media/front.png"], "model_asset_ids": ids}}).json()
        assert r["count"] == 3 and len(r["jobs"]) == 3
        res = wait_done(c, saleor)
        batch = [x for x in res if x["batch"] == r["batch"]]
        assert len(batch) == 3 and {x["label"].split("·")[-1].strip() for x in batch} == {"model 1/3", "model 2/3", "model 3/3"}
        c.put("/api/studio/settings", headers=H(saleor), json={"preferred_providers": {"tryon": ""}})
