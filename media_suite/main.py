import contextvars
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, Body, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from saleor_app.app import SaleorApp
from saleor_app.deps import saleor_domain_header, verify_saleor_domain
from saleor_app.schemas.core import DomainName, InstallData, WebhookData
from saleor_app.schemas.manifest import Extension, Manifest, MountType, TargetType
from saleor_app.schemas.utils import LazyPath, LazyUrl

from .optimizer_api import router as optimizer_router
from .studio_api import media_router, router as studio_router
from .db import db
from .saleor_api import SaleorAPI, SaleorAPIError
from .service import optimize_product
from .settings import settings


class AbsoluteUrl(LazyUrl):
    """LazyUrl that serializes as a plain string.

    Starlette >= 0.26 returns a URL object from ``request.url_for``; the
    framework passes it straight into the manifest, which then renders as
    ``{"_url": ...}`` and Saleor refuses the install.
    """

    def resolve(self):
        return str(super().resolve())


logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
logger = logging.getLogger(__name__)

if not settings.secret_key:
    raise RuntimeError("SECRET_KEY must be set (openssl rand -hex 32)")

STATIC_DIR = Path(__file__).parent / "static"

# The framework's install hook only receives the domain + token. Saleor also
# sends its API URL in a header, which we want to keep (Saleor may live under
# a path prefix). A contextvar carries it from the middleware into save_app_data.
_saleor_api_url: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "saleor_api_url", default=None
)


# --------------------------------------------------------------------------
# Framework hooks
# --------------------------------------------------------------------------
async def validate_domain(saleor_domain: DomainName) -> bool:
    if not settings.allowed_saleor_domains:
        return True
    return saleor_domain in settings.allowed_saleor_domains


async def save_app_data(saleor_domain: DomainName, auth_token: str, webhook_data: Optional[WebhookData]):
    scheme = "http" if settings.use_insecure_saleor_http else "https"
    api_url = _saleor_api_url.get() or f"{scheme}://{saleor_domain}/graphql/"
    db.save_installation(saleor_domain, auth_token, api_url, webhook_data)
    logger.info("Installed for %s (api=%s)", saleor_domain, api_url)


async def get_webhook_details(saleor_domain: DomainName) -> WebhookData:
    installation = db.get_installation(saleor_domain)
    if not installation or not installation.webhook_secret:
        # Unknown shop: return a random secret so signature verification fails.
        return WebhookData(webhook_id="", webhook_secret_key="missing")
    return WebhookData(
        webhook_id=installation.webhook_id or "",
        webhook_secret_key=installation.webhook_secret,
    )


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
manifest = Manifest(
    id=settings.app_id,
    name=settings.app_name,
    version=settings.app_version,
    about="Image Optimizer (resize, WebP/AVIF, compression) and AI Studio (AI-generated product photos, try-on, video) in one app.",
    permissions=["MANAGE_PRODUCTS"],
    data_privacy="Provider API keys are stored encrypted. Product images are sent to the AI provider you select in AI Studio.",
    data_privacy_url=AbsoluteUrl("app-page"),
    homepage_url=AbsoluteUrl("app-page"),
    support_url=AbsoluteUrl("app-page"),
    configuration_url=AbsoluteUrl("app-page"),
    app_url=AbsoluteUrl("app-page"),
    token_target_url=AbsoluteUrl("app-install"),
    extensions=[
        Extension(label="Image Optimizer", mount=MountType.NAVIGATION_CATALOG, target=TargetType.APP_PAGE,
                  permissions=["MANAGE_PRODUCTS"], url=LazyPath("optimizer-page")),
        Extension(label="AI Studio", mount=MountType.NAVIGATION_CATALOG, target=TargetType.APP_PAGE,
                  permissions=["MANAGE_PRODUCTS"], url=LazyPath("studio-page")),
    ],
)

app = SaleorApp(
    manifest=manifest,
    validate_domain=validate_domain,
    save_app_data=save_app_data,
    use_insecure_saleor_http=settings.use_insecure_saleor_http,
    development_auth_token=settings.development_auth_token or None,
    title=settings.app_name,
    version=settings.app_version,
)


@app.middleware("http")
async def capture_saleor_api_url(request: Request, call_next):
    token = _saleor_api_url.set(request.headers.get("saleor-api-url"))
    try:
        return await call_next(request)
    finally:
        _saleor_api_url.reset(token)


# --------------------------------------------------------------------------
# Dashboard page (rendered inside the Saleor dashboard iframe)
# --------------------------------------------------------------------------
@app.get("/", name="app-page", response_class=HTMLResponse, include_in_schema=False)
async def app_page():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


NO_STORE = {"Cache-Control": "no-store"}  # pages must always pick up new asset versions


@app.get("/optimizer", name="optimizer-page", response_class=HTMLResponse, include_in_schema=False)
async def optimizer_page():
    return HTMLResponse((STATIC_DIR / "optimizer" / "index.html").read_text(encoding="utf-8"), headers=NO_STORE)


@app.get("/studio", name="studio-page", response_class=HTMLResponse, include_in_schema=False)
async def studio_page():
    return HTMLResponse((STATIC_DIR / "studio" / "index.html").read_text(encoding="utf-8"), headers=NO_STORE)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------
# Webhook: optimize images as they are uploaded (opt-in per shop)
# --------------------------------------------------------------------------
PRODUCT_MEDIA_CREATED_SUBSCRIPTION = """
subscription {
  event {
    ... on ProductMediaCreated {
      issuingPrincipal { ... on App { id } ... on User { id } }
      recipient { id }
      productMedia { id type url product { id } }
    }
  }
}
"""

app.include_webhook_router(get_webhook_details=get_webhook_details)


@app.webhook_router.http_event_route(
    "PRODUCT_MEDIA_CREATED", subscription_query=PRODUCT_MEDIA_CREATED_SUBSCRIPTION
)
async def product_media_created(
    background_tasks: BackgroundTasks,
    payload: Any = Body(...),
    saleor_domain: str = Depends(saleor_domain_header),
):
    if isinstance(payload, list):  # legacy (non-subscription) payload shape
        payload = payload[0] if payload else {}
    media = (payload or {}).get("productMedia") or {}
    principal = (payload or {}).get("issuingPrincipal") or {}
    recipient = (payload or {}).get("recipient") or {}

    # Media we uploaded ourselves also triggers this event - don't loop.
    if principal and recipient and principal.get("id") == recipient.get("id"):
        return {"status": "ignored", "reason": "own upload"}
    if media.get("type") != "IMAGE" or not media.get("product"):
        return {"status": "ignored", "reason": "not a product image"}

    installation = db.get_installation(saleor_domain)
    if not installation:
        return {"status": "ignored", "reason": "not installed"}
    cfg = db.get_optimize_settings(saleor_domain)
    if not cfg.auto_optimize_new_uploads:
        return {"status": "ignored", "reason": "auto-optimize disabled"}

    background_tasks.add_task(
        optimize_product, installation, media["product"]["id"], cfg, [media["id"]], False
    )
    return {"status": "scheduled", "media": media["id"]}


# --------------------------------------------------------------------------
# Install endpoint
# --------------------------------------------------------------------------
WEBHOOK_CREATE = """
mutation WebhookCreate($input: WebhookCreateInput!) {
  webhookCreate(input: $input) {
    errors { field message code }
    webhook { id }
  }
}
"""


async def register_webhook(saleor_api_url: str, auth_token: str, target_url: str) -> Optional[WebhookData]:
    """Create our PRODUCT_MEDIA_CREATED webhook. Returns None (and logs why)
    if Saleor rejects it - the dashboard features work without it, only
    auto-optimize needs it."""
    secret = secrets.token_urlsafe(32)
    events = app.webhook_router.http_routes
    payload = {
        "name": settings.app_name,
        "targetUrl": target_url,
        "asyncEvents": list(events.keys()),
        "query": app.webhook_router.http_routes_subscriptions.get(next(iter(events))),
        "secretKey": secret,
        "isActive": True,
    }
    try:
        async with SaleorAPI(saleor_api_url, auth_token) as api:
            data = await api.execute(WEBHOOK_CREATE, {"input": payload})
    except SaleorAPIError as exc:
        logger.error("webhookCreate failed (GraphQL): %s", exc.errors or exc)
        return None
    result = data.get("webhookCreate") or {}
    if result.get("errors") or not result.get("webhook"):
        logger.error("webhookCreate rejected: %s", result.get("errors"))
        return None
    return WebhookData(webhook_id=result["webhook"]["id"], webhook_secret_key=secret)


async def install(
    request: Request,
    data: InstallData,
    _domain_is_valid=Depends(verify_saleor_domain),
    saleor_domain=Depends(saleor_domain_header),
):
    """Replaces saleor_app.endpoints.install (Copy of saleor_app.endpoints.install
    at heart, but it works with current Starlette and surfaces Saleor's
    webhook validation errors instead of crashing on them)."""
    scheme = "http" if settings.use_insecure_saleor_http else "https"
    api_url = _saleor_api_url.get() or f"{scheme}://{saleor_domain}/graphql/"
    target_url = str(request.url_for("handle-webhook"))
    webhook_data = await register_webhook(api_url, data.auth_token, target_url)
    await request.app.save_app_data(
        saleor_domain=saleor_domain, auth_token=data.auth_token, webhook_data=webhook_data
    )
    return {}


# Registered before the framework's own /install so ours wins (same path + name).
async def manifest_endpoint(request: Request):
    """Replaces the framework's manifest endpoint, which resolves URLs on the first
    request and then freezes them (a stray internal request would poison the manifest).
    This resolves them from the current request every time."""
    data = manifest.dict(by_alias=True, exclude_none=True)

    def resolve(value):
        if isinstance(value, LazyPath):
            return str(request.app.url_path_for(value.name))
        if isinstance(value, LazyUrl):
            return str(request.url_for(value.name))
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    return resolve(data)


app.configuration_router.get("/manifest", name="app-manifest")(manifest_endpoint)
app.configuration_router.post("/install", name="app-install")(install)

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.on_event("startup")
async def mark_interrupted_jobs():
    """AI jobs run inside the worker process; anything still running at startup was cut off by a restart."""
    with db._lock, db._conn:  # noqa: SLF001
        db._conn.execute("UPDATE jobs SET status='error', error='interrupted by app restart' WHERE status IN ('queued','running')")


app.include_saleor_app_routes()  # /configuration/manifest + /configuration/install
app.include_router(optimizer_router)
app.include_router(studio_router)
app.include_router(media_router)
