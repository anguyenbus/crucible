"""Service configuration via pydantic-settings.

All service-owned settings use the `PARSER_` env prefix (the repo's per-service
prefix convention, mirroring `services/ingestion/app/config.py`'s `INGESTION_`).
Standard AWS variables (`AWS_REGION`) stay bare.

This is the HTTP boundary's typed view of the knobs. The vendored
`parser_service` package reads a subset of these directly from `os.environ`
(`PARSER_MAX_PAGES`, `PARSER_MAX_INPUT_MB`, `PARSER_ESCALATION_ENGINE`,
`PARSER_SCAN_FASTPATH`, `PARSER_ESCALATION_ARBITRATION`, `BEDROCK_VLM_MODEL`);
bridging these settings onto those env names for the pipeline call is a later
task group's concern. Task Group 1 only needs the defaults to be correct.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PARSER_")

    # Escalation engine for gate-promoted pages. Textract is the deliberate
    # default (deterministic → better dedup predictability); `vlm` is selectable.
    escalation_engine: str = "textract"

    # VLM (Bedrock Claude) model used only when escalation_engine == "vlm".
    # AU-region cross-region inference profile. Maps onto BEDROCK_VLM_MODEL for
    # the vendored package.
    vlm_model: str = "au.anthropic.claude-sonnet-4-6"

    # Operational guardrails. Page cap × concurrency IS the POC spend bound.
    max_pages: int = 100
    # Input-size cap in MB; oversized inputs are refused with 413. Deliberately
    # 50 MB for the service (the vendored package's own default is 200 MB).
    max_input_mb: float = 50.0

    # Per-request spend guard. DISABLED by default (None): the page cap ×
    # concurrency bound is the POC ceiling, and a miscalibrated dollar meter that
    # silently degrades quality is worse than none. When set, feeds an advisory
    # cost summary only — it NEVER gates parsing.
    budget_usd: float | None = None

    # Provisional Textract LAYOUT+TABLES price. Advisory cost-summary input only,
    # NEVER a gate. Live ap-southeast-2 verification is an ops follow-up.
    textract_price_per_page_usd: float = 0.019

    # Idle-timeout knob (seconds without a per-page frame) for the streaming
    # path. This is an IDLE timeout, NOT a wall-clock total — a legitimate
    # 100-page parse runs ~100–190 min. Aligns with the BFF's 120 s idle constant.
    idle_timeout_seconds: int = 120

    # Concurrency = 1 in-flight parse per pod (torch/docling saturate the cores);
    # scale horizontally by replicas only.
    max_concurrent_parses: int = 1

    # Opt-in feature toggles — both default OFF (byte-identical to baseline).
    # scan_fastpath is documented as the first perf/cost knob to flip once the
    # corpus is confirmed scan-heavy ("OFF knowing why").
    scan_fastpath: bool = False
    escalation_arbitration: bool = False

    # Standard AWS variables stay unprefixed.
    aws_region: str = Field(default="ap-southeast-2", validation_alias="AWS_REGION")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
