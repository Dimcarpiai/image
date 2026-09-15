"""End-to-end against a fake Saleor GraphQL server (tokenVerify, products, media upload)."""
import json
import socket
import threading
import time
from io import BytesIO

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from image_optimizer.db import db
from image_optimizer.main import app
from image_optimizer import settings as settings_module


class FakeSaleor:
    def __init__(self):
        self.app = FastAPI()
        self.calls = []
        img = Image.new("RGB", (2500, 1500), (10, 200, 30))
        buf = BytesIO(); img.save(buf, "PNG")
        self.png = buf.getvalue()
        self.media = [{"id": "M1", "alt": "front", "type": "IMAGE", "url": None, "optimized": None},
                      {"id": "M2", "alt": "", "type": "VIDEO", "url": "https://youtu.be/x", "optimized": None}]
        self.uploaded = []
        self.webhooks = []

        @self.app.api_route("/media/{name}", methods=["GET", "HEAD"])
        async def media(name: str):
            from fastapi import Response
            return Response(self.png, media_type="image/png")

        @self.app.post("/graphql/")
        async def graphql(request: Request):
            ct = request.headers.get("content-type", "")
            files = {}
            if ct.startswith("multipart/form-data"):
                form = await request.form()
                body = json.loads(form["operations"])
                files["0"] = await form["0"].read()
                files["name"] = form["0"].filename
                files["ct"] = form["0"].content_type
            else:
                body = await request.json()
            q, v = body["query"], body.get("variables") or {}
            self.calls.append((q.split("(")[0].split()[-1], v))
            if "webhookCreate" in q:
                if getattr(self, "reject_webhook", False):
                    return {"data": {"webhookCreate": {"errors": [{"field": "query", "message": "bad", "code": "INVALID"}], "webhook": None}}}
                self.webhooks.append(v["input"])
                return {"data": {"webhookCreate": {"errors": [], "webhook": {"id": "WH1"}}}}
            if "tokenVerify" in q:
                return {"data": {"tokenVerify": {"isValid": v["token"] == "staff-jwt", "user": {"id": "U"}}}}
            if "query ProductsWithMedia" in q:
                return {"data": {"products": {"totalCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "edges": [{"node": {"id": "P1", "name": "Shirt", "media": [dict(m, thumb=m["url"]) for m in self.media]}}]}}}
            if "query ProductMedia" in q:
                return {"data": {"product": {"id": "P1", "name": "Shirt", "media": self.media}}}
            if "productMediaCreate" in q:
                self.uploaded.append(files)
                new = {"id": f"M{len(self.media)+1}", "alt": v["alt"], "type": "IMAGE", "url": self.base + "/media/new.webp", "optimized": None}
                self.media.append(new)
                return {"data": {"productMediaCreate": {"errors": [], "media": {"id": new["id"], "url": new["url"]}}}}
            if "productMediaDelete" in q:
                self.media = [m for m in self.media if m["id"] != v["id"]]
                return {"data": {"productMediaDelete": {"errors": []}}}
            if "productMediaReorder" in q:
                order = {mid: i for i, mid in enumerate(v["mediaIds"])}
                self.media.sort(key=lambda m: order[m["id"]])
                return {"data": {"productMediaReorder": {"errors": []}}}
            if "updatePrivateMetadata" in q:
                for m in self.media:
                    if m["id"] == v["id"]:
                        m["optimized"] = "true"
                return {"data": {"updatePrivateMetadata": {"errors": []}}}
            return {"errors": [{"message": f"unhandled query {q[:40]}"}]}

    def start(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.base = f"http://127.0.0.1:{self.port}"
        self.media[0]["url"] = self.base + "/media/front.png"
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)
        return self


@pytest.fixture(scope="module")
def saleor():
    settings_module.settings.use_insecure_saleor_http = True
    app.use_insecure_saleor_http = True
    fake = FakeSaleor().start()
    db.save_installation(f"127.0.0.1:{fake.port}", "app-token", fake.base + "/graphql/", None)
    yield fake


def _headers(saleor):
    return {"x-saleor-domain": f"127.0.0.1:{saleor.port}", "x-saleor-token": "staff-jwt"}


def test_rejects_invalid_token(saleor):
    c = TestClient(app)
    r = c.get("/api/settings", headers={**_headers(saleor), "x-saleor-token": "bad"})
    assert r.status_code == 400


def test_products_and_optimize_flow(saleor):
    c = TestClient(app)
    r = c.get("/api/products", headers=_headers(saleor))
    assert r.status_code == 200, r.text
    prod = r.json()["items"][0]
    assert prod["media"][0]["bytes"] == len(saleor.png)
    assert prod["media"][0]["optimized"] is False

    r = c.put("/api/settings", headers=_headers(saleor), json={"format": "webp", "quality": 80, "max_width": 1200, "max_height": 1200,
              "strip_metadata": True, "replace_original": True, "skip_if_not_smaller": True, "auto_optimize_new_uploads": False})
    assert r.status_code == 200

    r = c.post("/api/optimize", headers=_headers(saleor), json={"product_id": "P1"})
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    assert [x["status"] for x in res] == ["optimized", "skipped"]
    assert res[0]["new_size"] == [1200, 720]
    assert res[0]["optimized_bytes"] < res[0]["original_bytes"]

    # multipart upload happened with the right content type
    up = saleor.uploaded[0]
    assert up["ct"] == "image/webp" and up["name"].endswith("-optimized.webp")
    assert Image.open(BytesIO(up["0"])).format == "WEBP"

    # original deleted, new media in first position, tagged as optimized
    assert [m["id"] for m in saleor.media] == ["M3", "M2"]
    assert saleor.media[0]["optimized"] == "true"

    # second run is a no-op
    r = c.post("/api/optimize", headers=_headers(saleor), json={"product_id": "P1"})
    assert r.json()["results"][0]["reason"] == "already optimized"

    # stats recorded
    assert c.get("/api/capabilities", headers=_headers(saleor)).json()["stats"]["images"] == 1


def test_preview_endpoint(saleor):
    c = TestClient(app)
    r = c.post("/api/preview", headers=_headers(saleor), json={"url": saleor.base + "/media/front.png"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert int(r.headers["X-Optimized-Bytes"]) < int(r.headers["X-Original-Bytes"])


def test_install_registers_webhook(saleor):
    c = TestClient(app)
    r = c.post("/configuration/install", json={"auth_token": "app-token"},
               headers={"x-saleor-domain": f"127.0.0.1:{saleor.port}", "saleor-api-url": saleor.base + "/graphql/"})
    assert r.status_code == 200, r.text
    wh = saleor.webhooks[-1]
    assert wh["targetUrl"] == "http://testserver/webhook"
    assert wh["asyncEvents"] == ["PRODUCT_MEDIA_CREATED"]
    assert "ProductMediaCreated" in wh["query"]
    inst = db.get_installation(f"127.0.0.1:{saleor.port}")
    assert inst.webhook_id == "WH1" and inst.webhook_secret == wh["secretKey"]
    assert inst.saleor_api_url == saleor.base + "/graphql/"


def test_install_survives_webhook_rejection(saleor, monkeypatch):
    c = TestClient(app)
    orig = saleor.app.routes
    saleor.reject_webhook = True
    r = c.post("/configuration/install", json={"auth_token": "app-token"},
               headers={"x-saleor-domain": f"127.0.0.1:{saleor.port}", "saleor-api-url": saleor.base + "/graphql/"})
    saleor.reject_webhook = False
    assert r.status_code == 200
    assert db.get_installation(f"127.0.0.1:{saleor.port}").webhook_id is None
