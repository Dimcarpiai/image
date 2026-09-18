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

## Layout

| Path | Purpose |
|---|---|
| `media_suite/main.py` | manifest with two extensions, install (+ optimizer webhook), pages |
| `media_suite/optimizer_api.py`, `optimizer.py`, `service.py` | Image Optimizer |
| `media_suite/studio_api.py`, `jobs.py`, `providers/` | AI Studio |
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
