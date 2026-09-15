from fastapi.testclient import TestClient

from image_optimizer.db import db
from image_optimizer.main import app
from image_optimizer.optimizer import OptimizeSettings
from saleor_app.schemas.core import WebhookData

client = TestClient(app)


def test_manifest():
    data = client.get("/configuration/manifest").json()
    assert data["id"] == "saleor.image-optimizer"
    assert data["permissions"] == ["MANAGE_PRODUCTS"]
    assert data["appUrl"] == "http://testserver/"
    assert data["tokenTargetUrl"] == "http://testserver/configuration/install"
    assert data["extensions"][0]["mount"] == "NAVIGATION_CATALOG"
    assert data["extensions"][0]["target"] == "APP_PAGE"
    assert data["extensions"][0]["url"] == "/"


def test_app_page_and_static():
    assert "Image Optimizer" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_db_roundtrip():
    db.save_installation("shop.example.com", "tok", "https://shop.example.com/graphql/", WebhookData(webhook_id="W", webhook_secret_key="s3cret"))
    inst = db.get_installation("shop.example.com")
    assert inst.saleor_api_url == "https://shop.example.com/graphql/"
    db.save_settings("shop.example.com", OptimizeSettings(quality=50))
    assert db.get_settings("shop.example.com").quality == 50


def test_webhook_rejects_bad_signature():
    db.save_installation("shop.example.com", "tok", "https://shop.example.com/graphql/", WebhookData(webhook_id="W", webhook_secret_key="s3cret"))
    r = client.post(
        "/webhook",
        json={"productMedia": {"id": "M", "type": "IMAGE", "product": {"id": "P"}}},
        headers={"x-saleor-domain": "shop.example.com", "x-saleor-event": "product_media_created", "x-saleor-signature": "nope"},
    )
    assert r.status_code == 401


def test_webhook_ignores_own_uploads_and_disabled_auto():
    import hashlib, hmac, json
    db.save_installation("shop.example.com", "tok", "https://shop.example.com/graphql/", WebhookData(webhook_id="W", webhook_secret_key="s3cret"))
    body = json.dumps({
        "issuingPrincipal": {"id": "APP1"}, "recipient": {"id": "APP1"},
        "productMedia": {"id": "M", "type": "IMAGE", "product": {"id": "P"}},
    }).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    headers = {"x-saleor-domain": "shop.example.com", "x-saleor-event": "product_media_created",
               "x-saleor-signature": sig, "content-type": "application/json"}
    r = client.post("/webhook", content=body, headers=headers)
    assert r.json()["reason"] == "own upload"

    body = json.dumps({
        "issuingPrincipal": {"id": "USER1"}, "recipient": {"id": "APP1"},
        "productMedia": {"id": "M", "type": "IMAGE", "product": {"id": "P"}},
    }).encode()
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    headers["x-saleor-signature"] = sig
    r = client.post("/webhook", content=body, headers=headers)
    assert r.json()["reason"] == "auto-optimize disabled"
