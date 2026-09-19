from typing import List

from pydantic import BaseSettings


class Settings(BaseSettings):
    app_id: str = "saleor.media-suite"
    app_name: str = "Media Suite"
    app_version: str = "1.3.3"

    secret_key: str = ""                      # encrypts provider keys, signs media URLs
    allowed_saleor_domains_raw: str = ""
    use_insecure_saleor_http: bool = False
    development_auth_token: str = ""
    database_path: str = "./data/app.sqlite3"
    data_dir: str = "./data"
    debug: bool = False

    max_download_bytes: int = 40 * 1024 * 1024
    max_upload_bytes: int = 25 * 1024 * 1024
    http_timeout: int = 180
    job_timeout: int = 900

    @property
    def allowed_saleor_domains(self) -> List[str]:
        return [d.strip() for d in self.allowed_saleor_domains_raw.split(",") if d.strip()]

    class Config:
        env_file = ".env"
        fields = {"allowed_saleor_domains_raw": {"env": "ALLOWED_SALEOR_DOMAINS"}}


settings = Settings()
