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

- **Product Builder** — Catalog → Product Builder: upload supplier photos or pick AI Studio results →
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
- **1.0.0 — catalog-production workflow.** AI Studio is now: *Select product → Select task → Add references →
  Generate → Publish*. Providers, models, sizes and variations live under *Advanced settings*; the base prompt is
  built from Saleor data (title, category, colour, material, attributes), so nothing is typed twice.
  - **Tasks**: Product photo · On model · Colour variants · Product pack · Edit image · Video.
  - **Product Lock**: shape, fabric, pattern, collar, buttons, logo, colour — locked parts are instructed to stay identical.
  - **Model Lock**: on any on-model result → *Keep this model*; every later on-model job reuses that person.
  - **Colour variants**: reads variant colours from Saleor and recolours an approved image per colour, tagging each
    result with its variant so *Add to variant* is one click.
  - **Packs** (one button + selector): Product (front/back/side/detail), Model (front/side/walking/close-up),
    Marketing (studio/lifestyle/social).
  - **Typed references**: Product, Model, Fabric, Style (*use style from another product* — camera, background,
    lighting, crop), Logo. **Pose presets**, **backgrounds** (white/grey/beige/transparent/custom), **logo control**
    (keep/remove/add), **partial edits** (remove logo, change colour, fix collar/sleeve, change trousers/background).
  - **Publishing** per result: Add to product · Add to variant · Set as thumbnail · Approve · Delete (results remember
    their product and variant). **Bulk**: one action for all selected products. **Looks**: collection consistency
    (same model, background, pose, locks). **Quality check** (Ready / Needs review + issue) via the vision LLM.
    **Version history** V1/V2/V3 per image with *Use as source*.
- **1.1.0 — SKUs & QR codes.** Edit details → *Generate SKUs* fills every variant from the pattern in Settings
  (`{brand}-{style}-{color:3}-{size}`, duplicates suffixed); *QR codes* shows a QR per product and per variant built from
  the storefront URL pattern (`https://shop/p/{slug}?variant={sku}`), downloadable as PNG with the SKU printed under it.
  API: `POST /api/studio/skus`, `GET /api/studio/qr?url=…&label=…`, `GET /api/studio/qr/product/{id}`.
- **0.9.0 — throughput features & new layout.** AI Studio is now four tabs: *Studio* (products with bulk selection,
  references, generation, results), *Review* (queue with Approve / Main / Other product / Reject and a keyboard
  *Compare view*: ← → A M R), *Library*, *Settings*. Plus:
  - *Generate pack for selected* — tick products in the list and run the ★ presets for all of them (products with an
    approved on-model image skip the try-on step);
  - *Auto-run on new uploads* — when a product gets its first image, the default pack runs into the review queue;
  - *Smart defaults* — prompt pre-filled from product attributes, try-on category from product type; presets accept
    `{product} {color} {material} {category}` placeholders;
  - *Default model photo per product type* (fallback `*`);
  - *Cost & time estimate* before generating, *daily budget* cap (HTTP 402 when exceeded), spend tracking;
  - *Clean uploads* — background removal + white square via Stability (optional);
  - *Job queue*: 4 parallel workers, automatic retry on transient provider errors;
  - *Notifications* — Slack/Teams-style webhook on pack completion and when the review queue reaches a threshold.
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
