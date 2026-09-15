from typing import List

from pydantic import BaseSettings


class Settings(BaseSettings):
    app_id: str = "saleor.image-optimizer"
    app_name: str = "Image Optimizer"
    app_version: str = "0.1.0"

    # Comma separated; kept as a plain string because pydantic's BaseSettings
    # would otherwise try to JSON-decode the env var and crash on "a.com,b.com".
    allowed_saleor_domains_raw: str = ""
    use_insecure_saleor_http: bool = False
    development_auth_token: str = ""
    database_path: str = "./data/app.sqlite3"
    debug: bool = False

    # Hard limits, independent of per-shop settings
    max_download_bytes: int = 40 * 1024 * 1024
    http_timeout: int = 60

    @property
    def allowed_saleor_domains(self) -> List[str]:
        return [d.strip() for d in self.allowed_saleor_domains_raw.split(",") if d.strip()]

    class Config:
        env_file = ".env"
        fields = {"allowed_saleor_domains_raw": {"env": "ALLOWED_SALEOR_DOMAINS"}}


settings = Settings()
