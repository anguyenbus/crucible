"""Task Group 2: settings load with the INGESTION_ prefix and sane defaults."""

from app.config import Settings


def test_settings_defaults(monkeypatch):
    for var in ("INGESTION_INDEX_NAME", "INGESTION_CHUNK_TOKENS", "INGESTION_OPENSEARCH_HOST"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings()

    assert settings.index_name == "genai-ingestion-md"
    assert settings.chunk_tokens == 800
    assert settings.chunk_overlap == 120
    assert settings.max_chunks_per_doc > 0
    assert settings.search_top_k == 5
    assert settings.knn_weight == 0.5
    assert settings.keyword_weight == 0.5


def test_settings_read_ingestion_prefixed_env_and_bare_aws_region(monkeypatch):
    monkeypatch.setenv("INGESTION_INDEX_NAME", "custom-index")
    monkeypatch.setenv("INGESTION_CHUNK_TOKENS", "512")
    monkeypatch.setenv("INGESTION_OPENSEARCH_HOST", "example.es.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    settings = Settings()

    assert settings.index_name == "custom-index"
    assert settings.chunk_tokens == 512
    assert settings.opensearch_host == "example.es.amazonaws.com"
    assert settings.aws_region == "us-east-1"


def test_parser_url_default_targets_internal_parser_service(monkeypatch):
    """Task Group 5: the compose default resolves to the east-west-only parser."""
    monkeypatch.delenv("INGESTION_PARSER_URL", raising=False)

    settings = Settings()

    # Default must match the internal compose service name/port (never a host
    # URL) — ingestion reaches the parser over the internal compose network.
    assert settings.parser_url == "http://parser:8000"


def test_parser_url_env_override(monkeypatch):
    """`INGESTION_PARSER_URL` overrides the compose default (per-deploy wiring)."""
    monkeypatch.setenv("INGESTION_PARSER_URL", "http://parser.internal:9000")

    settings = Settings()

    assert settings.parser_url == "http://parser.internal:9000"
