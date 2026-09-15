import contextvars
import logging
from pathlib import Path
from typing import Any, Optional

from collections import defaultdict

from fastapi import BackgroundTasks, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from saleor_app.app import SaleorApp
from saleor_app.deps import saleor_domain_header, verify_saleor_domain
from saleor_app.errors import InstallAppError
from saleor_app.install import install_app
from saleor_app.saleor.exceptions import GraphQLError
from saleor_app.schemas.core import DomainName, InstallData, WebhookData
from saleor_app.schemas.manifest import Extension, Manifest, MountType, TargetType
from saleor_app.schemas.utils import LazyPath, LazyUrl

from .api import router as api_router
from .db import db
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
    about="Resizes, converts (WebP/AVIF) and compresses product images so your storefront loads faster.",
    permissions=["MANAGE_PRODUCTS"],
    data_privacy="No personal data is stored. Product images are processed in memory.",
    data_privacy_url=AbsoluteUrl("app-page"),
    homepage_url=AbsoluteUrl("app-page"),
    support_url=AbsoluteUrl("app-page"),
    configuration_url=AbsoluteUrl("app-page"),
    app_url=AbsoluteUrl("app-page"),
    token_target_url=AbsoluteUrl("app-install"),
    extensions=[
        Extension(
            label="Image Optimizer",
            mount=MountType.NAVIGATION_CATALOG,
            target=TargetType.APP_PAGE,
            permissions=["MANAGE_PRODUCTS"],
            url=LazyPath("app-page"),
        )
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
    cfg = db.get_settings(saleor_domain)
    if not cfg.auto_optimize_new_uploads:
        return {"status": "ignored", "reason": "auto-optimize disabled"}

    background_tasks.add_task(
        optimize_product, installation, media["product"]["id"], cfg, [media["id"]], False
    )
    return {"status": "scheduled", "media": media["id"]}


# --------------------------------------------------------------------------
# Install endpoint
# --------------------------------------------------------------------------
async def install(
    request: Request,
    data: InstallData,
    _domain_is_valid=Depends(verify_saleor_domain),
    saleor_domain=Depends(saleor_domain_header),
):
    """Copy of saleor_app.endpoints.install with one fix: ``request.url_for``
    returns a URL object on current Starlette, which the framework uses as a
    dict key (unhashable -> 500 on install). We stringify it."""
    events = defaultdict(list)
    router = getattr(request.app, "webhook_router", None)
    if router is not None:
        target = str(request.url_for("handle-webhook"))
        for event_type in router.http_routes:
            events[target].append((event_type, router.http_routes_subscriptions.get(event_type)))

    webhook_data = None
    if events:
        try:
            webhook_data = await install_app(
                saleor_domain=saleor_domain,
                auth_token=data.auth_token,
                manifest=request.app.manifest,
                events=events,
                use_insecure_saleor_http=request.app.use_insecure_saleor_http,
            )
        except (InstallAppError, GraphQLError) as exc:
            logger.error("Install failed for %s: %s", saleor_domain, exc)
            raise HTTPException(status_code=403, detail="Incorrect token or not enough permissions")

    await request.app.save_app_data(
        saleor_domain=saleor_domain, auth_token=data.auth_token, webhook_data=webhook_data
    )
    return {}


# Registered before the framework's own /install so ours wins (same path + name).
app.configuration_router.post("/install", name="app-install")(install)

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
app.include_saleor_app_routes()  # /configuration/manifest + /configuration/install
app.include_router(api_router)
