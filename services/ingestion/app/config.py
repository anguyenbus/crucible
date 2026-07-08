"""Service configuration via pydantic-settings.

All service-owned settings use the `INGESTION_` env prefix (the repo's
per-service prefix convention). Standard AWS variables (`AWS_REGION`) stay
bare. This service deliberately owns its own settings and does not read the
eval service's `.env` material.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INGESTION_")

    # OpenSearch (eval-poc VPC domain; override via INGESTION_OPENSEARCH_HOST)
    opensearch_host: str = (
        "vpc-eval-poc-cvvwd6y6gsjdyyrdodrfi7p22y.ap-southeast-2.es.amazonaws.com"
    )
    index_name: str = "genai-ingestion-md"
    search_pipeline_name: str = "hybrid-search-pipeline"

    # Embedding (Bedrock Titan v2)
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dimensions: int = 1024

    # Chunking (tiktoken cl100k). Strategies: "recursive" (LangChain
    # RecursiveCharacterTextSplitter, splits at natural separators) or
    # "fixed" (hard token windows, ignores separators).
    chunk_strategy: str = "recursive"
    chunk_tokens: int = 800
    chunk_overlap: int = 120
    max_chunks_per_doc: int = 100

    # Search defaults (named hybrid-search-pipeline carries the 0.5/0.5 weights)
    search_top_k: int = 5
    knn_weight: float = 0.5
    keyword_weight: float = 0.5

    # Standard AWS variables stay unprefixed
    aws_region: str = Field(default="ap-southeast-2", validation_alias="AWS_REGION")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
