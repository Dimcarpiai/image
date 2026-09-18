# Saleor Media Suite

One Saleor app, one domain, one installation — two dashboard tools under **Catalog**:

- **Image Optimizer** — resize, convert (WebP/AVIF/JPEG/PNG), compress and replace product images; optional
  automatic optimization of new uploads (webhook); before/after preview; savings stats.
- **AI Studio** — generate new product images and clips with the AI provider you pick per generation:
  scenes from a prompt (OpenAI gpt-image-2.5 / Gemini 3.x Image / Stability AI background-relight, Ultra, SD 3.5), virtual try-on on a model photo
  (fal.ai FASHN / Image-Apps / FLUX 2, or OpenAI/Gemini), image-to-video (Kling 3, Veo 3, Seedance 2, Stable Video Diffusion).
  Results can be added to the product as media. Provider keys are entered in the app and stored encrypted.

Built on [mirumee/saleor-app-framework-python](https://github.com/mirumee/saleor-app-framework-python) with
the compatibility fixes for current Starlette/Saleor 3.21 applied.

Optional fifth provider **Local GPU (CatVTON)**: point it at your own try-on server (see the separate
`catvton-server` package) via `https://<tunnel-url>|<token>` in the API keys panel. CatVTON is CC BY-NC-SA (non-commercial).

- **Product Builder** (API only for now; the dashboard page is disabled in 0.8.0) — upload supplier photos or pick AI Studio results →
  *Draft with AI* (name, DE/EN descriptions, attributes, SEO, alt text; needs an OpenAI or Gemini key) →
  variant matrix (size × colour …) with SKU pattern (`{brand}-{style}-{color:3}-{size}`), price per channel,
  stock per warehouse → creates the product, variants, channel listings, images (per-variant assignment) and the
  German translation in one click.
- **Presets & packs** (AI Studio) — save prompt/provider/model combinations; *Generate pack* runs every preset
  marked ★ for the selected product (default pack: studio-white relight, lifestyle scene, on-model FASHN).
- **Review queue** (AI Studio) — generated images wait for approval; *Approve* attaches them to the product,
  *Reject* deletes them. Nothing is attached automatically.
- **Clone product** (AI Studio, 0.5.0) — from a generated image: *Clone product…* creates a new Saleor product as a copy
  of the current one (type, category, attributes, description, channels, variants, prices) with a new name, new colour
  value (product + variants), colour code swapped in the SKUs (`ROS-POLO-BUR-S` → `ROS-POLO-NAV-S`), stock 0 unless copied,
  and the image attached. *Add to another product…* attaches a generated image to any existing product.
- **Import from a page or URL** (AI Studio, 0.6.0) — paste a Pinterest pin, supplier page or direct image URL; the app lists
  the images it finds, you tick the ones to import as references, or click *Clone product with this* on one to create a
  colourway from that picture. Imported images are stored in the app (visible in Product Builder too).
- **Edit details** (AI Studio, 0.7.0) — edit the selected product in place: name, slug, category, description, SEO,
  product attributes, German translation, and per-variant SKU / price per channel / stock per warehouse. Product images
  can be removed with the ✕ on each tile.
- **Storefront revalidation** — Builder settings → *Storefront revalidate URL* (+ secret). The app POSTs
  `{"productId", "slug", "reason", "secret"}` with `Authorization: Bearer <secret>` after a product is created,
  an image is approved/attached, or the optimizer changed media, so cached storefront pages refresh.

## Layout

| Path | Purpose |
|---|---|
| `media_suite/main.py` | manifest with two extensions, install (+ optimizer webhook), pages |
| `media_suite/optimizer_api.py`, `optimizer.py`, `service.py` | Image Optimizer |
| `media_suite/studio_api.py`, `jobs.py`, `providers/` | AI Studio (incl. review queue, presets, packs) |
| `media_suite/builder_api.py`, `llm.py`, `storefront.py` | Product Builder, AI drafting, storefront notifications |
| `media_suite/saleor_api.py`, `db.py`, `crypto.py`, `settings.py` | shared |
| `media_suite/static/optimizer/`, `static/studio/`, `static/app.css` | dashboard pages |

## Deploy

```bash
cp .env.example .env
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env       # required
# server that already runs Caddy in another compose project:
NETWORK=<other-project>_edge docker compose -f docker-compose.prod.yaml up -d --build
```
Add to that Caddyfile and reload/restart Caddy:
```
media.example.com {
    encode zstd gzip
    reverse_proxy media-suite:8080
}
```
Install from the dashboard: `https://media.example.com/configuration/manifest`.

Uninstall the old standalone Image Optimizer first if it is installed; this app replaces it (same features,
new app id). Optimizer settings need to be set once again.

Files (model photos, generated media, SQLite) live in the `media-suite-data` volume.

## Tests

```bash
pip install pytest pytest-asyncio "httpx<0.28"
pytest
```
19 tests: optimizer image processing, provider request building, manifest/install, and full flows for both
tools against a fake Saleor and fake AI providers. The real OpenAI/Gemini/fal integrations follow the current
docs but were not run with live keys.

## Notes

- `SECRET_KEY` must not change afterwards (saved provider keys would become unreadable).
- One gunicorn worker: AI jobs run in-process. For heavy use move `jobs.py` to a queue.
- Videos can't be attached to Saleor products (Saleor only accepts YouTube/Vimeo links); download them.
