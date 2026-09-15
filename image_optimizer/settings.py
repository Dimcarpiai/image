from typing import List

from pydantic import BaseSettings, validator


class Settings(BaseSettings):
    app_id: str = "saleor.image-optimizer"
    app_name: str = "Image Optimizer"
    app_version: str = "0.1.0"

    allowed_saleor_domains: List[str] = []
    use_insecure_saleor_http: bool = False
    development_auth_token: str = ""
    database_path: str = "./data/app.sqlite3"
    debug: bool = False

    # Hard limits, independent of per-shop settings
    max_download_bytes: int = 40 * 1024 * 1024
    http_timeout: int = 60

    @validator("allowed_saleor_domains", pre=True)
    def _split_domains(cls, value):
        if isinstance(value, str):
            return [d.strip() for d in value.split(",") if d.strip()]
        return value

    class Config:
        env_file = ".env"


settings = Settings()
