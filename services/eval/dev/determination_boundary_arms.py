"""
LIVE Bedrock arm for the determination-boundary calibration suite (dev/-only, AWS-gated).

The calibration LIBRARY (``app.phoenix.determination_boundary``) is pure and
arm-agnostic. This module supplies the one live piece: a
:class:`~app.phoenix.guardrail_ab.GuardArm` that runs the pod's COMMITTED
``self_check_output`` prompt over an answer on REAL Bedrock Haiku and returns the
rail's verdict.

WHAT IT MEASURES, EXACTLY. The output lane's decision IS this prompt: NeMo renders
``self_check_output`` with the answer bound to ``{{ bot_response }}``, calls the
guard model at ``max_tokens: 4`` and ``lowest_temperature: 0.0``, and reads the
one-word reply through ``is_content_safe`` — "Yes" ⇒ BLOCK. This arm performs that
same render / call / parse against the same model id and region, reading the
prompt text out of ``services/guardrail/config/prompts.yml`` on disk. It is the
same measurement technique task 3.4 used for the input-token delta, so the item-8
numbers and the token delta are quoted from the same lane.

TWO REFUSALS, BOTH DELIBERATE — "never a draft", enforced on the ARTIFACT:

  * :func:`assert_measures_pinned_artifact` — the on-disk ``config/`` is
    re-hashed with the pod's OWN digest function and must equal the
    ``config_dir_digest`` pinned in the orchestrator config. That hash IS the
    identity of the reviewed artifact (``config/`` is a hashed artifact, and the
    pod itself refuses to serve on the same mismatch), so equality is the
    strongest available form of "the number describes the shipped prompt, not a
    draft" — strictly stronger than a working-tree cleanliness check, which would
    pass for any committed-but-unpinned edit. :func:`prompt_git_status` is
    reported alongside it as provenance.
  * :func:`read_pod_config_version` feeds the harness's version key, and the
    harness refuses to report against a build other than
    ``determination_boundary.POD_CONFIG_VERSION``.

AWS-GATED. Every row is a real, paid Bedrock call, so this cannot run unattended
in CI. Nothing here is imported at ``app`` scope; boto3 is imported lazily inside
the client factory so the module stays importable (and unit-testable with an
injected fake client) with no credentials present.

One-way dependency rule (honoured): this ``dev/`` module MAY import ``app.*``;
nothing in ``app`` imports ``dev``.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml
from app.phoenix.guardrail_ab import GuardOutcome

# The repo layout: this file is services/eval/dev/determination_boundary_arms.py.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
GUARDRAIL_CONFIG_DIR: Final[Path] = REPO_ROOT / "services" / "guardrail" / "config"
PROMPTS_YML: Final[Path] = GUARDRAIL_CONFIG_DIR / "prompts.yml"
ORCHESTRATOR_CONFIG: Final[Path] = (
    REPO_ROOT
    / "services"
    / "orchestrator"
    / "app"
    / "configs"
    / "legal-rag-default-1.8.0.yaml"
)

OUTPUT_TASK: Final[str] = "self_check_output"
ANSWER_PLACEHOLDER: Final[str] = "{{ bot_response }}"

# Pinned by services/guardrail/config/config.yml + app/settings.py. The guard
# model is Haiku and is NEVER the generator or the eval judge (model-role
# separation is a standing invariant).
GUARD_MODEL_ID: Final[str] = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
GUARD_REGION: Final[str] = "ap-southeast-2"
GUARD_TEMPERATURE: Final[float] = 0.0


class ArtifactDriftError(RuntimeError):
    """The on-disk ``config/`` is not the artifact the orchestrator config pins."""

    def __init__(self, computed: str, pinned: str) -> None:
        """Build the message from the recomputed and pinned digests."""
        super().__init__(
            f"config_dir_digest drift: services/guardrail/config/ hashes to "
            f"{computed} but the orchestrator config pins {pinned}. The item-8 "
            "calibration measures the REVIEWED, PINNED prompt artifact — a number "
            "reported against a drifted config/ describes a prompt that was never "
            "pinned. Re-pin (`uv run python -m app.config_digest`) or revert before "
            "running."
        )
        self.computed = computed
        self.pinned = pinned


def _load_pod_config_digest_module() -> Any:
    """
    Load the pod's OWN ``app.config_digest`` by path (dev/-only, no duplication).

    Re-implementing the digest here would be a second definition of the artifact's
    identity, and a drift between the two would silently pass this gate. The pod's
    module is pure stdlib, so loading it by path costs nothing and keeps ONE
    definition of the hash.
    """
    import importlib.util

    module_path = REPO_ROOT / "services" / "guardrail" / "app" / "config_digest.py"
    spec = importlib.util.spec_from_file_location("guardrail_config_digest", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the pod's config_digest module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_measures_pinned_artifact(
    config_dir: Path = GUARDRAIL_CONFIG_DIR, pin_path: Path = ORCHESTRATOR_CONFIG
) -> str:
    """
    Refuse to run unless the on-disk ``config/`` IS the pinned, reviewed artifact.

    Returns:
        The verified ``config_dir_digest``.

    Raises:
        ArtifactDriftError: When the recomputed digest differs from the pin.

    """
    computed = str(_load_pod_config_digest_module().compute_config_dir_digest(config_dir))
    pinned = read_pod_config_dir_digest(pin_path)
    if computed != pinned:
        raise ArtifactDriftError(computed, pinned)
    return computed


def prompt_git_status(path: Path = PROMPTS_YML) -> str:
    """
    Return the prompt file's ``git status --porcelain`` line (run-report provenance).

    Empty means the pinned artifact is also committed to git. A non-empty status
    is NOT a failure — the binding "never a draft" gate is the digest pin — but it
    is recorded with the run so a reader knows the artifact's VCS state at the time
    the number was produced.
    """
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def read_pod_config_version(path: Path = ORCHESTRATOR_CONFIG) -> str:
    """Read ``guardrails.nemo.config_version`` from the pinned orchestrator config."""
    return str(_nemo_pins(path)["config_version"])


def read_pod_config_dir_digest(path: Path = ORCHESTRATOR_CONFIG) -> str:
    """Read the pinned ``config_dir_digest`` — the hash of the artifact measured."""
    return str(_nemo_pins(path)["config_dir_digest"])


def _nemo_pins(path: Path) -> dict[str, Any]:
    """Return the ``guardrails.nemo`` pin block from an orchestrator pipeline config."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(document["guardrails"]["nemo"])


def load_output_prompt(path: Path = PROMPTS_YML) -> tuple[str, int]:
    """
    Load the committed ``self_check_output`` prompt body and its ``max_tokens`` pin.

    Returns:
        The prompt body verbatim and the pinned ``max_tokens`` (4), so the arm
        cannot drift from the shipped decision budget.

    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in document["prompts"]:
        if entry["task"] == OUTPUT_TASK:
            return str(entry["content"]), int(entry["max_tokens"])
    raise KeyError(f"{OUTPUT_TASK} not found in {path}")


def _build_bedrock_client(region: str) -> Any:
    """Build the bedrock-runtime client from the AMBIENT credential chain (lazy import)."""
    import boto3

    return boto3.client("bedrock-runtime", region_name=region)


@dataclass
class BedrockSelfCheckOutputArm:
    """
    The output rail's decision on REAL Bedrock Haiku, one answer at a time.

    Attributes:
        config_ref: Provenance label for the Phoenix experiment (the pod
            ``config_version`` under measurement).
        prompt: The committed ``self_check_output`` body (from
            :func:`load_output_prompt`).
        max_tokens: The pinned decision budget (4).
        model_id: The guard model id.
        region: The Bedrock region.
        client: An injected bedrock-runtime client; built lazily from the ambient
            credential chain when omitted (tests inject a fake and never call AWS).

    """

    config_ref: str
    prompt: str
    max_tokens: int = 4
    model_id: str = GUARD_MODEL_ID
    region: str = GUARD_REGION
    client: Any = field(default=None)

    def _client(self) -> Any:
        """Return the injected client, building one on first use."""
        if self.client is None:
            self.client = _build_bedrock_client(self.region)
        return self.client

    def check(
        self, *, question: str, answer: str, chunks: tuple[str, ...]
    ) -> GuardOutcome:
        """
        Run the committed output rail over ONE answer and return its verdict.

        ``question`` / ``chunks`` are unused: ``self_check_output`` is a policy
        self-check of the ANSWER alone (grounding against chunks is the separate
        ``self_check_facts`` rail, untouched this slice).
        """
        _ = (question, chunks)
        rendered = self.prompt.replace(ANSWER_PLACEHOLDER, answer)
        started = time.perf_counter()
        response = self._client().converse(
            modelId=self.model_id,
            messages=[{"role": "user", "content": [{"text": rendered}]}],
            inferenceConfig={
                "maxTokens": self.max_tokens,
                "temperature": GUARD_TEMPERATURE,
            },
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        usage = response.get("usage", {})
        return GuardOutcome(
            blocked=_is_block(_reply_text(response)),
            latency_ms=latency_ms,
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
            model_id=self.model_id,
        )


def _reply_text(response: dict[str, Any]) -> str:
    """Concatenate the assistant reply's text blocks from a ``converse`` response."""
    content = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(str(block.get("text", "")) for block in content)


def _is_block(reply: str) -> bool:
    """
    Decide BLOCK exactly as NeMo's ``is_content_safe`` output parser does.

    Mirrors ``nemoguardrails.llm.output_parsers.is_content_safe`` verbatim in
    behaviour (it cannot be imported here — the pod resolves its own venv): runs
    of non-word characters collapse to a space, only the FIRST TWO words are
    considered, "yes"/"unsafe" ⇒ block, "no"/"safe" ⇒ allow, and anything
    unrecognised falls back to BLOCK.

    Reproducing this precisely is load-bearing. Haiku answers this prompt with
    ``**Yes**``; a naive ``startswith("yes")`` reads that as an ALLOW and reports
    a catastrophic under-block that the rail does not actually have. The
    normalisation step is what turns ``**Yes**`` into ``yes``.
    """
    normalised = re.sub(r"\W+", " ", reply.lower().strip())
    first_words = normalised.split(" ")[:2]
    for token, content_safe in (("safe", True), ("unsafe", False), ("yes", False), ("no", True)):
        if token in first_words:
            return not content_safe
    return True


def build_live_arm(config_ref: str, *, client: Any = None) -> BedrockSelfCheckOutputArm:
    """
    Build the live arm from the COMMITTED prompt, refusing to measure a draft.

    Args:
        config_ref: The pod ``config_version`` under measurement (experiment
            provenance).
        client: Optional injected bedrock-runtime client (tests).

    Returns:
        The configured :class:`BedrockSelfCheckOutputArm`.

    Raises:
        ArtifactDriftError: When ``config/`` is not the pinned, reviewed artifact.

    """
    assert_measures_pinned_artifact()
    prompt, max_tokens = load_output_prompt()
    return BedrockSelfCheckOutputArm(
        config_ref=config_ref, prompt=prompt, max_tokens=max_tokens, client=client
    )
