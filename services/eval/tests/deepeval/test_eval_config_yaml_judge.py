"""Task Group 7: tests for the eval_config.yaml judge-block shape.

These tests load the dev-fixtures eval_config.yaml and assert that the
top-level global ``judge:`` block is the read path consumed by
``get_deepeval_config`` (and that the former per-dataset
``datasets.*.deepeval`` judge block is no longer read).
"""

from pathlib import Path

import pytest
import yaml

from app.deepeval.bedrock_provider import DEFAULT_JUDGE_MODEL, get_deepeval_config

# eval_config.yaml lives in the dev fixtures (services/eval/dev/fixtures/).
_EVAL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "dev" / "fixtures" / "eval_config.yaml"

# These tests assert the SHAPE of the dev-fixtures eval_config.yaml -- a dev
# artifact, not one of the migrating buckets (kernel/service/contracts). When the
# file is absent (e.g. in the migration-rehearsal skeleton, which copies only the
# buckets), skip rather than error: the skeleton stays hermetic and these
# config-shape assertions remain a dev-fixtures concern.
if not _EVAL_CONFIG_PATH.exists():
    pytest.skip(
        "eval_config.yaml not present in dev/fixtures (config-shape "
        "tests do not apply when the dev fixtures are absent)",
        allow_module_level=True,
    )

# Env vars that influence judge/region resolution; cleared per-test so suite
# ordering cannot leak state into these YAML-shape assertions.
_JUDGE_ENV_VARS = (
    "EVAL_JUDGE_PROVIDER",
    "EVAL_JUDGE_MODEL",
    "EVAL_GENERATOR_MODEL",
    "DEEPEVAL_MAX_CONCURRENT",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@pytest.fixture(autouse=True)
def _clean_judge_env(monkeypatch):
    for var in _JUDGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def _load_raw_config() -> dict:
    """Load eval_config.yaml WITHOUT env expansion (shape-only inspection)."""
    return yaml.safe_load(_EVAL_CONFIG_PATH.read_text())


def test_eval_config_has_top_level_judge_block():
    """eval_config.yaml exposes a top-level global ``judge:`` block."""
    config = _load_raw_config()

    assert "judge" in config, "eval_config.yaml must have a top-level judge: block"
    judge = config["judge"]
    # Shape consumed by get_deepeval_config (region is NOT in this block).
    assert judge["provider"] == "bedrock"
    assert judge["model"] == DEFAULT_JUDGE_MODEL == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert judge["enabled"] is True
    assert judge["temperature"] == 0.0
    assert judge["max_concurrent"] == 10
    assert "region" not in judge


def test_per_dataset_deepeval_judge_block_removed():
    """The per-dataset datasets.*.deepeval judge block is gone (dead read path)."""
    config = _load_raw_config()

    for name, ds in config.get("datasets", {}).items():
        assert "deepeval" not in ds, (
            f"datasets.{name}.deepeval should be removed; the judge: block is "
            "the only judge config read path"
        )


def test_get_deepeval_config_reads_actual_yaml_judge_block(monkeypatch):
    """get_deepeval_config returns the judge config from the actual YAML block."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    config = _load_raw_config()

    result = get_deepeval_config(config)

    assert result["enabled"] is True
    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert result["temperature"] == 0.0
    assert result["max_concurrent"] == 10
    assert result["region"] == "ap-southeast-2"


def test_get_deepeval_config_ignores_per_dataset_deepeval(monkeypatch):
    """Even if a stray per-dataset deepeval block exists, it is NOT the read path."""
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    config = _load_raw_config()
    # Inject a stray per-dataset judge block that disagrees with the global one.
    config["datasets"]["legal_rag_bench"]["deepeval"] = {
        "judge_model": "gpt-4o",
        "judge_model_provider": "openai",
    }

    result = get_deepeval_config(config)

    # The top-level judge: block wins; the per-dataset block is ignored.
    assert result["judge_model_provider"] == "bedrock"
    assert result["judge_model"] == "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
