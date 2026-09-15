"""Tiny SQLite store: one row per installed Saleor instance + per-shop settings.

Kept deliberately simple (no SQLAlchemy/alembic) so the app runs anywhere with
a writable volume. Swap for a real database if you run many shops.
"""
import json
import os
import sqlite3
import threading
from typing import Optional

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
                    domain TEXT PRIMARY KEY,
                    auth_token TEXT NOT NULL,
                    saleor_api_url TEXT NOT NULL,
                    webhook_id TEXT,
                    webhook_secret TEXT
                );
                CREATE TABLE IF NOT EXISTS shop_settings (
                    domain TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS optimize_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    media_id TEXT NOT NULL,
                    new_media_id TEXT,
                    original_bytes INTEGER,
                    optimized_bytes INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    # -- installations -----------------------------------------------------
    def save_installation(
        self,
        domain: str,
        auth_token: str,
        saleor_api_url: str,
        webhook_data: Optional[WebhookData],
    ):
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO installations (domain, auth_token, saleor_api_url, webhook_id, webhook_secret)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    auth_token = excluded.auth_token,
                    saleor_api_url = excluded.saleor_api_url,
                    webhook_id = excluded.webhook_id,
                    webhook_secret = excluded.webhook_secret
                """,
                (
                    domain,
                    auth_token,
                    saleor_api_url,
                    webhook_data.webhook_id if webhook_data else None,
                    webhook_data.webhook_secret_key if webhook_data else None,
                ),
            )

    def get_installation(self, domain: str) -> Optional[Installation]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM installations WHERE domain = ?", (domain,)
            ).fetchone()
        return Installation(**dict(row)) if row else None

    def delete_installation(self, domain: str):
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM installations WHERE domain = ?", (domain,))
            self._conn.execute("DELETE FROM shop_settings WHERE domain = ?", (domain,))

    # -- per-shop settings -------------------------------------------------
    def get_settings(self, domain: str) -> OptimizeSettings:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM shop_settings WHERE domain = ?", (domain,)
            ).fetchone()
        if not row:
            return OptimizeSettings()
        return OptimizeSettings(**json.loads(row["data"]))

    def save_settings(self, domain: str, data: OptimizeSettings):
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO shop_settings (domain, data) VALUES (?, ?)
                ON CONFLICT(domain) DO UPDATE SET data = excluded.data
                """,
                (domain, data.json()),
            )

    # -- history -----------------------------------------------------------
    def log_optimization(
        self,
        domain: str,
        product_id: str,
        media_id: str,
        new_media_id: Optional[str],
        original_bytes: int,
        optimized_bytes: int,
    ):
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO optimize_log
                    (domain, product_id, media_id, new_media_id, original_bytes, optimized_bytes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (domain, product_id, media_id, new_media_id, original_bytes, optimized_bytes),
            )

    def stats(self, domain: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS images,
                       COALESCE(SUM(original_bytes), 0) AS original_bytes,
                       COALESCE(SUM(optimized_bytes), 0) AS optimized_bytes
                FROM optimize_log WHERE domain = ?
                """,
                (domain,),
            ).fetchone()
        return dict(row)


db = Database(settings.database_path)
