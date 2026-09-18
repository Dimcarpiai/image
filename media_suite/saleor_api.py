"""GraphQL calls this app makes against Saleor, using the app token from install.

The framework's own SaleorClient only speaks JSON; product media upload needs the
GraphQL multipart spec, so we have a small client of our own here.
"""
import asyncio
import json
import logging
from typing import Dict, List, Optional

import aiohttp

from .settings import settings

logger = logging.getLogger(__name__)

META_KEY_OPTIMIZED = "image_optimizer.optimized"
META_KEY_ORIGINAL_BYTES = "image_optimizer.original_bytes"
META_KEY_SOURCE_MEDIA = "image_optimizer.source_media_id"


class SaleorAPIError(Exception):
    def __init__(self, message: str, errors=None):
        super().__init__(message)
        self.errors = errors or []


PRODUCTS_WITH_MEDIA = """
query ProductsWithMedia($first: Int!, $after: String, $search: String) {
  products(first: $first, after: $after, filter: { search: $search }, sortBy: { field: NAME, direction: ASC }) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        media {
          id
          alt
          type
          url
          thumb: url(size: 256)
          optimized: privateMetafield(key: "%s")
        }
      }
    }
  }
}
""" % META_KEY_OPTIMIZED

PRODUCT_MEDIA = """
query ProductMedia($id: ID!) {
  product(id: $id) {
    id
    name
    media {
      id
      alt
      type
      url
      optimized: privateMetafield(key: "%s")
    }
  }
}
""" % META_KEY_OPTIMIZED

PRODUCT_MEDIA_CREATE = """
mutation ProductMediaCreate($product: ID!, $image: Upload!, $alt: String) {
  productMediaCreate(input: { product: $product, image: $image, alt: $alt }) {
    errors { field message code }
    media { id url }
  }
}
"""

PRODUCT_MEDIA_DELETE = """
mutation ProductMediaDelete($id: ID!) {
  productMediaDelete(id: $id) {
    errors { field message code }
  }
}
"""

PRODUCT_MEDIA_REORDER = """
mutation ProductMediaReorder($productId: ID!, $mediaIds: [ID!]!) {
  productMediaReorder(productId: $productId, mediaIds: $mediaIds) {
    errors { field message code }
  }
}
"""

UPDATE_PRIVATE_METADATA = """
mutation UpdatePrivateMetadata($id: ID!, $input: [MetadataInput!]!) {
  updatePrivateMetadata(id: $id, input: $input) {
    errors { field message code }
  }
}
"""


STUDIO_PRODUCTS = """
query StudioProducts($first: Int!, $after: String, $search: String) {
  products(first: $first, after: $after, filter: { search: $search }, sortBy: { field: NAME, direction: ASC }) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges { node { id name thumbnail(size: 256) { url } category { name } media { id alt type url thumb: url(size: 512) } } }
  }
}
"""


class SaleorAPI:
    def __init__(self, api_url: str, auth_token: str):
        self.api_url = api_url
        self.auth_token = auth_token
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "User-Agent": f"{settings.app_id}/{settings.app_version}",
            },
            timeout=aiohttp.ClientTimeout(total=settings.http_timeout),
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # -- low level ----------------------------------------------------------
    async def execute(self, query: str, variables: Optional[dict] = None) -> dict:
        async with self._session.post(
            self.api_url, json={"query": query, "variables": variables or {}}
        ) as resp:
            body = await self._read_json(resp)
        if body.get("errors"):
            raise SaleorAPIError("GraphQL error", body["errors"])
        return body.get("data") or {}

    async def execute_upload(
        self, query: str, variables: dict, file_var: str, data: bytes, filename: str, content_type: str
    ) -> dict:
        """GraphQL multipart request (https://github.com/jaydenseric/graphql-multipart-request-spec)."""
        variables = {**variables, file_var: None}
        form = aiohttp.FormData()
        form.add_field("operations", json.dumps({"query": query, "variables": variables}))
        form.add_field("map", json.dumps({"0": [f"variables.{file_var}"]}))
        form.add_field("0", data, filename=filename, content_type=content_type)
        async with self._session.post(self.api_url, data=form) as resp:
            body = await self._read_json(resp)
        if body.get("errors"):
            raise SaleorAPIError("GraphQL error", body["errors"])
        return body.get("data") or {}

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> dict:
        try:
            return await resp.json()
        except aiohttp.ContentTypeError:
            text = (await resp.text())[:300]
            raise SaleorAPIError(f"Saleor returned HTTP {resp.status} (non-JSON): {text}")

    @staticmethod
    def _raise_on_errors(payload: dict, mutation: str):
        errors = (payload.get(mutation) or {}).get("errors") or []
        if errors:
            raise SaleorAPIError(f"{mutation} failed", errors)

    # -- products & media ---------------------------------------------------
    async def list_products(self, first: int = 20, after: Optional[str] = None, search: str = "") -> dict:
        data = await self.execute(
            PRODUCTS_WITH_MEDIA, {"first": first, "after": after, "search": search or None}
        )
        return data["products"]

    async def list_products_studio(self, first: int = 20, after: Optional[str] = None, search: str = "") -> dict:
        data = await self.execute(STUDIO_PRODUCTS, {"first": first, "after": after, "search": search or None})
        return data["products"]

    async def get_product(self, product_id: str) -> Optional[dict]:
        data = await self.execute(PRODUCT_MEDIA, {"id": product_id})
        return data.get("product")

    async def download(self, url: str) -> bytes:
        # Media URLs are public; don't send the app token to a third-party CDN.
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.http_timeout)
        ) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                length = resp.content_length or 0
                if length > settings.max_download_bytes:
                    raise SaleorAPIError(f"image too large ({length} bytes)")
                data = await resp.read()
                if len(data) > settings.max_download_bytes:
                    raise SaleorAPIError(f"image too large ({len(data)} bytes)")
                return data

    async def head_sizes(self, urls: List[str]) -> Dict[str, Optional[int]]:
        """Best-effort Content-Length lookup for a batch of media URLs."""
        results: Dict[str, Optional[int]] = {}
        sem = asyncio.Semaphore(8)

        async def one(session, url):
            async with sem:
                try:
                    async with session.head(url, allow_redirects=True) as resp:
                        results[url] = resp.content_length if resp.status == 200 else None
                except Exception:  # noqa: BLE001 - purely informational
                    results[url] = None

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            await asyncio.gather(*(one(session, u) for u in urls))
        return results

    async def create_media(self, product_id: str, data: bytes, filename: str, content_type: str, alt: str) -> dict:
        payload = await self.execute_upload(
            PRODUCT_MEDIA_CREATE,
            {"product": product_id, "alt": alt or ""},
            "image",
            data,
            filename,
            content_type,
        )
        self._raise_on_errors(payload, "productMediaCreate")
        return payload["productMediaCreate"]["media"]

    async def delete_media(self, media_id: str):
        payload = await self.execute(PRODUCT_MEDIA_DELETE, {"id": media_id})
        self._raise_on_errors(payload, "productMediaDelete")

    async def reorder_media(self, product_id: str, media_ids: List[str]):
        payload = await self.execute(
            PRODUCT_MEDIA_REORDER, {"productId": product_id, "mediaIds": media_ids}
        )
        self._raise_on_errors(payload, "productMediaReorder")

    async def mark_optimized(self, media_id: str, original_bytes: int, source_media_id: str):
        payload = await self.execute(
            UPDATE_PRIVATE_METADATA,
            {
                "id": media_id,
                "input": [
                    {"key": META_KEY_OPTIMIZED, "value": "true"},
                    {"key": META_KEY_ORIGINAL_BYTES, "value": str(original_bytes)},
                    {"key": META_KEY_SOURCE_MEDIA, "value": source_media_id},
                ],
            },
        )
        self._raise_on_errors(payload, "updatePrivateMetadata")


# --------------------------------------------------------------------------
# Product Builder: catalog metadata + creation
# --------------------------------------------------------------------------
BUILDER_META = """
query BuilderMeta {
  channels { id name slug currencyCode }
  warehouses(first: 50) { edges { node { id name } } }
  productTypes(first: 100) {
    edges { node {
      id name
      productAttributes { id name slug inputType valueRequired choices(first: 100) { edges { node { name } } } }
      assignedVariantAttributes { attribute { id name slug inputType valueRequired choices(first: 100) { edges { node { name } } } } variantSelection }
    } }
  }
}
"""

CATEGORIES_PAGE = """
query BuilderCategories($after: String) {
  categories(first: 100, after: $after) { pageInfo { hasNextPage endCursor } edges { node { id name parent { name } } } }
}
"""

PRODUCT_CREATE = """
mutation BuilderProductCreate($input: ProductCreateInput!) {
  productCreate(input: $input) { product { id name slug } errors { field message code } }
}
"""

PRODUCT_CHANNEL_LISTING_UPDATE = """
mutation BuilderProductChannelListing($id: ID!, $input: ProductChannelListingUpdateInput!) {
  productChannelListingUpdate(id: $id, input: $input) { errors { field message code channels } }
}
"""

VARIANT_BULK_CREATE = """
mutation BuilderVariants($product: ID!, $variants: [ProductVariantBulkCreateInput!]!) {
  productVariantBulkCreate(product: $product, variants: $variants, errorPolicy: REJECT_EVERYTHING) {
    productVariants { id sku name }
    errors { field message code index }
  }
}
"""

VARIANT_MEDIA_ASSIGN = """
mutation BuilderAssignMedia($mediaId: ID!, $variantId: ID!) {
  variantMediaAssign(mediaId: $mediaId, variantId: $variantId) { errors { field message } }
}
"""

PRODUCT_TRANSLATE = """
mutation BuilderTranslate($id: ID!, $lang: LanguageCodeEnum!, $input: TranslationInput!) {
  productTranslate(id: $id, languageCode: $lang, input: $input) { errors { field message } }
}
"""

PRODUCT_SLUG = """
query BuilderProductSlug($id: ID!) { product(id: $id) { id slug name } }
"""


def editorjs(paragraphs) -> str:
    """Saleor stores descriptions as EditorJS JSON."""
    blocks = [{"type": "paragraph", "data": {"text": p}} for p in paragraphs if p]
    return json.dumps({"time": 0, "blocks": blocks, "version": "2.22.2"})


class BuilderMixin:
    async def builder_meta(self) -> dict:
        d = await self.execute(BUILDER_META)
        cats, after = [], None
        for _ in range(20):   # up to 2000 categories
            page = (await self.execute(CATEGORIES_PAGE, {"after": after}))["categories"]
            cats += [e["node"] for e in page["edges"]]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        d["categories"] = {"edges": [{"node": c} for c in cats]}
        def attr(a):
            return {"id": a["id"], "name": a["name"], "slug": a["slug"], "inputType": a["inputType"], "valueRequired": a["valueRequired"],
                    "values": [c["node"]["name"] for c in (a.get("choices") or {}).get("edges", [])]}
        return {
            "channels": d["channels"],
            "warehouses": [e["node"] for e in d["warehouses"]["edges"]],
            "categories": [{"id": e["node"]["id"], "name": e["node"]["name"],
                            "path": (e["node"]["parent"]["name"] + " / " if e["node"].get("parent") else "") + e["node"]["name"]}
                           for e in d["categories"]["edges"]],
            "productTypes": [{"id": e["node"]["id"], "name": e["node"]["name"],
                              "productAttributes": [attr(a) for a in e["node"]["productAttributes"] or []],
                              "variantAttributes": [attr(v["attribute"]) for v in e["node"]["assignedVariantAttributes"] or []]}
                             for e in d["productTypes"]["edges"]],
        }

    async def create_product(self, input_data: dict) -> dict:
        r = (await self.execute(PRODUCT_CREATE, {"input": input_data}))["productCreate"]
        if r["errors"]:
            raise SaleorAPIError("productCreate failed", r["errors"])
        return r["product"]

    async def update_product_channels(self, product_id: str, channels: list):
        r = (await self.execute(PRODUCT_CHANNEL_LISTING_UPDATE, {"id": product_id, "input": {"updateChannels": channels}}))["productChannelListingUpdate"]
        if r["errors"]:
            raise SaleorAPIError("productChannelListingUpdate failed", r["errors"])

    async def bulk_create_variants(self, product_id: str, variants: list) -> list:
        r = (await self.execute(VARIANT_BULK_CREATE, {"product": product_id, "variants": variants}))["productVariantBulkCreate"]
        if r["errors"]:
            raise SaleorAPIError("productVariantBulkCreate failed", r["errors"])
        return r["productVariants"]

    async def assign_variant_media(self, media_id: str, variant_id: str):
        r = (await self.execute(VARIANT_MEDIA_ASSIGN, {"mediaId": media_id, "variantId": variant_id}))["variantMediaAssign"]
        if r["errors"]:
            raise SaleorAPIError("variantMediaAssign failed", r["errors"])

    async def translate_product(self, product_id: str, lang: str, name: str, description_json: str, seo_title: str, seo_description: str):
        r = (await self.execute(PRODUCT_TRANSLATE, {"id": product_id, "lang": lang, "input": {
            "name": name, "description": description_json, "seoTitle": seo_title, "seoDescription": seo_description}}))["productTranslate"]
        if r["errors"]:
            raise SaleorAPIError("productTranslate failed", r["errors"])

    async def product_slug(self, product_id: str) -> Optional[dict]:
        return (await self.execute(PRODUCT_SLUG, {"id": product_id})).get("product")


for _name, _fn in vars(BuilderMixin).items():
    if callable(_fn) and not _name.startswith("__"):
        setattr(SaleorAPI, _name, _fn)
