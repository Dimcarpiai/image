"""SQLite store shared by both tools: installations, optimizer settings, provider keys, assets, jobs, history."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from saleor_app.schemas.core import WebhookData

from .optimizer import OptimizeSettings
from .settings import settings


class Installation(BaseModel):
    domain: str
    auth_token: str
    saleor_api_url: str
    webhook_id: Optional[str] = None
    webhook_secret: Optional[str] = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()

    def _migrate(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS installations (
                    domain TEXT PRIMARY KEY, auth_token TEXT NOT NULL, saleor_api_url TEXT NOT NULL,
                    webhook_id TEXT, webhook_secret TEXT
                );
                CREATE TABLE IF NOT EXISTS optimize_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, product_id TEXT NOT NULL,
                    media_id TEXT NOT NULL, new_media_id TEXT, original_bytes INTEGER, optimized_bytes INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS shop_settings (domain TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, domain TEXT NOT NULL, kind TEXT NOT NULL,
                    product_id TEXT, mime TEXT NOT NULL, path TEXT NOT NULL,
                    label TEXT, meta TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS assets_domain_kind ON assets(domain, kind, created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, domain TEXT NOT NULL, status TEXT NOT NULL,
                    mode TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    product_id TEXT, input TEXT NOT NULL, asset_ids TEXT NOT NULL DEFAULT '[]',
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_domain ON jobs(domain, created_at);
                """
            )

    # -- installations ------------------------------------------------------
    def save_installation(self, domain: str, auth_token: str, saleor_api_url: str, webhook_data: Optional[WebhookData] = None):
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO installations (domain, auth_token, saleor_api_url, webhook_id, webhook_secret) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET auth_token=excluded.auth_token, saleor_api_url=excluded.saleor_api_url,
                   webhook_id=excluded.webhook_id, webhook_secret=excluded.webhook_secret""",
                (domain, auth_token, saleor_api_url,
                 webhook_data.webhook_id if webhook_data else None, webhook_data.webhook_secret_key if webhook_data else None),
            )

    def get_installation(self, domain: str) -> Optional[Installation]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM installations WHERE domain = ?", (domain,)).fetchone()
        return Installation(**dict(row)) if row else None

    # -- settings -----------------------------------------------------------
    def get_settings(self, domain: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM shop_settings WHERE domain = ?", (domain,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def save_settings(self, domain: str, data: Dict[str, Any]):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO shop_settings (domain, data) VALUES (?, ?) ON CONFLICT(domain) DO UPDATE SET data=excluded.data",
                (domain, json.dumps(data)),
            )

    def get_optimize_settings(self, domain: str) -> OptimizeSettings:
        return OptimizeSettings(**self.get_settings(domain).get("optimizer", {}))

    def save_optimize_settings(self, domain: str, data: OptimizeSettings):
        current = self.get_settings(domain)
        current["optimizer"] = json.loads(data.json())
        self.save_settings(domain, current)

    def log_optimization(self, domain: str, product_id: str, media_id: str, new_media_id: Optional[str], original_bytes: int, optimized_bytes: int):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO optimize_log (domain, product_id, media_id, new_media_id, original_bytes, optimized_bytes) VALUES (?, ?, ?, ?, ?, ?)",
                (domain, product_id, media_id, new_media_id, original_bytes, optimized_bytes))

    def stats(self, domain: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS images, COALESCE(SUM(original_bytes),0) AS original_bytes, COALESCE(SUM(optimized_bytes),0) AS optimized_bytes FROM optimize_log WHERE domain = ?",
                (domain,)).fetchone()
        return dict(row)

    # -- assets -------------------------------------------------------------
    def add_asset(self, domain: str, kind: str, mime: str, path: str, product_id: Optional[str] = None,
                  label: Optional[str] = None, meta: Optional[dict] = None) -> dict:
        asset = {
            "id": uuid.uuid4().hex, "domain": domain, "kind": kind, "product_id": product_id,
            "mime": mime, "path": path, "label": label, "meta": json.dumps(meta or {}), "created_at": now(),
        }
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO assets (id, domain, kind, product_id, mime, path, label, meta, created_at) VALUES (:id,:domain,:kind,:product_id,:mime,:path,:label,:meta,:created_at)",
                asset,
            )
        return self._asset_out(asset)

    def get_asset(self, domain: str, asset_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM assets WHERE id = ? AND domain = ?", (asset_id, domain)).fetchone()
        return self._asset_out(dict(row)) if row else None

    def list_assets(self, domain: str, kind: Optional[str] = None, product_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        q, params = "SELECT * FROM assets WHERE domain = ?", [domain]
        if kind:
            q += " AND kind = ?"; params.append(kind)
        if product_id:
            q += " AND product_id = ?"; params.append(product_id)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._asset_out(dict(r)) for r in rows]

    def set_asset_meta(self, domain: str, asset_id: str, patch: dict):
        asset = self.get_asset(domain, asset_id)
        if not asset:
            return
        meta = {**asset["meta"], **patch}
        with self._lock, self._conn:
            self._conn.execute("UPDATE assets SET meta = ? WHERE id = ?", (json.dumps(meta), asset_id))

    def delete_asset(self, domain: str, asset_id: str) -> Optional[dict]:
        asset = self.get_asset(domain, asset_id)
        if asset:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return asset

    @staticmethod
    def _asset_out(a: dict) -> dict:
        a = dict(a)
        a["meta"] = json.loads(a["meta"]) if isinstance(a["meta"], str) else a["meta"]
        return a

    # -- jobs ---------------------------------------------------------------
    def create_job(self, domain: str, mode: str, provider: str, model: str, product_id: Optional[str], input_data: dict) -> dict:
        job = {
            "id": uuid.uuid4().hex, "domain": domain, "status": "queued", "mode": mode, "provider": provider,
            "model": model, "product_id": product_id, "input": json.dumps(input_data), "asset_ids": "[]",
            "error": None, "created_at": now(), "updated_at": now(),
        }
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, domain, status, mode, provider, model, product_id, input, asset_ids, error, created_at, updated_at) "
                "VALUES (:id,:domain,:status,:mode,:provider,:model,:product_id,:input,:asset_ids,:error,:created_at,:updated_at)",
                job,
            )
        return self._job_out(job)

    def update_job(self, job_id: str, status: str, asset_ids: Optional[List[str]] = None, error: Optional[str] = None):
        with self._lock, self._conn:
            if asset_ids is not None:
                self._conn.execute("UPDATE jobs SET status=?, asset_ids=?, error=?, updated_at=? WHERE id=?",
                                   (status, json.dumps(asset_ids), error, now(), job_id))
            else:
                self._conn.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now(), job_id))

    def get_job(self, domain: str, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ? AND domain = ?", (job_id, domain)).fetchone()
        return self._job_out(dict(row)) if row else None

    def list_jobs(self, domain: str, product_id: Optional[str] = None, limit: int = 50) -> List[dict]:
        q, params = "SELECT * FROM jobs WHERE domain = ?", [domain]
        if product_id:
            q += " AND product_id = ?"; params.append(product_id)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._job_out(dict(r)) for r in rows]

    @staticmethod
    def _job_out(j: dict) -> dict:
        j = dict(j)
        j["input"] = json.loads(j["input"]) if isinstance(j["input"], str) else j["input"]
        j["asset_ids"] = json.loads(j["asset_ids"]) if isinstance(j["asset_ids"], str) else j["asset_ids"]
        return j


db = Database(settings.database_path)
