"""Task Group 3: index/pipeline body builders + idempotent create_index smoke."""

import subprocess
import sys
from pathlib import Path

import pytest

from app.clients.opensearch import (
    KNN_EF_SEARCH,
    build_index_body,
    build_search_pipeline_body,
)

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_index_body_has_knn_settings_and_field_types():
    body = build_index_body()

    index_settings = body["settings"]["index"]
    assert index_settings["knn"] is True
    assert index_settings["knn.algo_param.ef_search"] == KNN_EF_SEARCH

    props = body["mappings"]["properties"]
    assert props["content"]["type"] == "text"
    vector = props["content_vector"]
    assert vector["type"] == "knn_vector"
    assert vector["dimension"] == 1024
    assert vector["method"]["name"] == "hnsw"
    assert vector["method"]["space_type"] == "cosinesimil"
    assert vector["method"]["engine"] == "faiss"
    for field in ("doc_id", "source_uri", "sha256"):
        assert props[field]["type"] == "keyword"
    assert props["chunk_index"]["type"] == "integer"
    assert props["created_at"]["type"] == "date"


def test_search_pipeline_body_is_min_max_with_knn_then_keyword_weights():
    body = build_search_pipeline_body()

    processor = body["phase_results_processors"][0]["normalization-processor"]
    assert processor["normalization"]["technique"] == "min_max"
    assert processor["combination"]["technique"] == "arithmetic_mean"
    assert processor["combination"]["parameters"]["weights"] == [0.5, 0.5]

    custom = build_search_pipeline_body(knn_weight=0.7, keyword_weight=0.3)
    custom_processor = custom["phase_results_processors"][0]["normalization-processor"]
    assert custom_processor["combination"]["parameters"]["weights"] == [0.7, 0.3]


@pytest.mark.requires_aws
def test_create_index_is_idempotent_across_two_runs():
    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SERVICE_ROOT / "scripts" / "create_index.py")],
            capture_output=True,
            text=True,
            timeout=120,
        )

    first = run()
    assert first.returncode == 0, (
        f"first run exited {first.returncode}\n"
        f"stdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    )

    second = run()
    assert second.returncode == 0, (
        f"second run exited {second.returncode}\n"
        f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
    )
    # Verified no-op: both the index and the pipeline are skipped on re-run.
    assert second.stdout.count("skipping (no-op)") == 2, second.stdout
