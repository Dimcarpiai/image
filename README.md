# Saleor Image Optimizer

A Saleor app, built on [mirumee/saleor-app-framework-python](https://github.com/mirumee/saleor-app-framework-python),
that installs into the dashboard as an extension (Catalog → **Image Optimizer**) and optimizes product images:

- resize to a maximum width/height
- convert to WebP, AVIF, JPEG or PNG (or keep the format)
- recompress with a chosen quality, strip EXIF/ICC metadata
- replace the original media in place (order preserved) or add the optimized copy next to it
- optional: optimize every newly uploaded product image automatically (`PRODUCT_MEDIA_CREATED` webhook)
- before/after preview with real byte counts, per-shop settings, running totals of bytes saved

Optimized images are tagged in the media's private metadata (`image_optimizer.optimized=true`) so they are
never processed twice.

## How it fits together

```
Saleor dashboard ──iframe──▶ GET /                     static/index.html + app.js
        │  AppBridge handshake gives the page a staff token
        │
        └──▶ /api/*  (X-Saleor-Domain + X-Saleor-Token headers, verified by the framework)
                │
                └──▶ Saleor GraphQL (app token from install):
                     products/media  →  download  →  Pillow  →  productMediaCreate (multipart)
                                                            →  productMediaDelete + productMediaReorder
                                                            →  updatePrivateMetadata
Saleor ──webhook──▶ POST /webhook  (HMAC signature verified by the framework)
```

| Path | Purpose |
|---|---|
| `image_optimizer/main.py` | `SaleorApp` setup, manifest, dashboard page, webhook |
| `image_optimizer/api.py` | JSON API used by the dashboard page |
| `image_optimizer/service.py` | fetch → optimize → upload → replace flow |
| `image_optimizer/optimizer.py` | pure Pillow image processing (unit tested) |
| `image_optimizer/saleor_api.py` | GraphQL client incl. multipart upload |
| `image_optimizer/db.py` | SQLite store for installs, settings, history |
| `image_optimizer/static/` | the page rendered inside the dashboard |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn image_optimizer.main:app --reload --port 8080 --proxy-headers --forwarded-allow-ips='*'
```

The dashboard has to reach your machine over https, so expose it with a tunnel
(`ngrok http 8080`, `cloudflared tunnel --url http://localhost:8080`, ...). Then in the dashboard:
**Extensions → Add Extension → Install from manifest URL** and paste

```
https://<your-tunnel-host>/configuration/manifest
```

The manifest builds its URLs from the incoming request, which is why `--proxy-headers` matters.

With Docker: `docker compose up --build` (fill in `.env` first).

### Configuration (`.env`)

| Variable | Meaning |
|---|---|
| `ALLOWED_SALEOR_DOMAINS` | Comma separated list of shops allowed to install. Empty = any (dev only). |
| `USE_INSECURE_SALEOR_HTTP` | `true` when Saleor itself runs on plain http (local docker). |
| `DATABASE_PATH` | SQLite file for installs/settings. |
| `DEVELOPMENT_AUTH_TOKEN` | Lets you call `/api/*` without a dashboard token while developing. Never set in production. |

### Tests

```bash
pip install pytest pytest-asyncio "httpx<0.28"
pytest
```

`tests/test_integration.py` runs the whole optimize flow against a fake Saleor GraphQL server,
including the multipart upload, delete, reorder and metadata calls.

## Notes and caveats

- **The framework is unmaintained** (last commit May 2023). It requires `pydantic<2` and `fastapi<0.100`,
  which this project pins. Saleor 3.2x still sends the `X-Saleor-*` headers and honours webhook
  `secretKey` (HMAC) that the framework depends on, so it works today; when Saleor 4.0 drops those,
  this app will need its own install/webhook handling.
- One framework bug is worked around in `main.py` (`AbsoluteUrl`): with current Starlette the framework
  would emit manifest URLs as `{"_url": ...}` objects and the install fails.
- Saleor already serves resized thumbnails via `ProductMedia.url(size:, format:)`. This app optimizes the
  *originals* those thumbnails are generated from, which is what you need when your storefront links the
  original URL or when uploaded originals are unnecessarily large.
- Replacing a media item gives it a **new ID and URL**. Anything that stored the old media ID (a CMS,
  a search index) needs to re-sync. Turn off *Replace the original image* if that is a problem.
- Processing runs in the request handler, one product per request. Fine for catalogs of a few thousand
  images; for very large catalogs move `optimize_product` into a task queue.
- AVIF output requires Pillow ≥ 11.3 with AVIF support (included in the official wheels). The UI disables
  the option when unavailable.
