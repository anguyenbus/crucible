"""Service configuration via pydantic-settings.

All service-owned settings use the `WEBUI_` env prefix (the repo's per-service
prefix convention, mirroring `services/ingestion/app/config.py`). Standard AWS
variables (`AWS_REGION`) stay bare. The BFF reaches its sibling services ONLY
over their env-var URLs -- it never imports their Python packages, and it makes
no compose-only service-name assumptions (the independent-K8s-pod future must
not require a code change).

There are no import-time side effects: settings are read lazily via the
`@lru_cache`d `get_settings()` accessor, and env values always win over the
in-code defaults below.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBUI_")

    # Sibling services, consumed over HTTP only (dependency firewall).
    ingestion_url: str = "http://localhost:8001"
    orchestrator_url: str = "http://localhost:8000"

    # Upload storage. `upload_bucket` is used when AWS credentials are present;
    # otherwise uploads fall back to a local absolute path under `upload_dir`.
    upload_bucket: str = ""
    upload_dir: str = "webui-uploads"

    # SQLite store file (create-if-absent at startup; lives on a volume).
    db_path: str = "webui.db"

    # Comma-separated allowed browser origins for CORS. The BFF is purpose-built
    # WITH CORS so the frontend can reach it browser-direct (`NEXT_PUBLIC_BFF_URL`).
    cors_origins: str = "http://localhost:3000"

    # Standard AWS variables stay unprefixed.
    aws_region: str = Field(default="ap-southeast-2", validation_alias="AWS_REGION")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
