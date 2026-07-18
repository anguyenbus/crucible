"""
Infrastructure clients (OpenSearch retrieval, Bedrock embed/generate, guard).

Import discipline: ALL infra libraries (boto3, opensearch-py, OpenTelemetry,
httpx) are confined to this package and injected into the pipeline stages — the
``app.orchestrator`` stage modules must never import them directly (enforced
by the ``stages-pure`` import-linter contract in ``pyproject.toml``).

:class:`AppClients` is the bundle FastAPI lifespan constructs ONCE (via
:func:`build_app_clients`) and stores on ``app.state``; the router threads
the instances into the pure stages per request. Tests substitute mock
instances on ``app.state`` BEFORE lifespan runs — lifespan only constructs
clients when none are pre-installed, so pytest never touches AWS.

OpenTelemetry still does not appear here — clients return PLAIN DATA and the
router owns span creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.clients.bedrock import TITAN_EMBEDDING_DIMENSIONS, BedrockClient
from app.clients.errors import OpenSearchNotReadyError
from app.clients.guardrail import GuardClassifier
from app.clients.nemo_guard import NemoGuardClient
from app.clients.opensearch import OpenSearchSearchClient
from app.config import DEFAULT_PIPELINE_CONFIG_REF, Settings, resolve_pipeline_config


@dataclass
class AppClients:
    """
    The once-per-process client bundle living on ``app.state.clients``.

    ``search`` is ``None`` when the OpenSearch client could not be
    constructed (no endpoint configured, ``_meta`` mismatch, or the domain
    was unreachable at startup) — the service then renders not-ready on
    ``readyz`` and maps ``/query`` to 502 (dependency: opensearch), instead
    of crashing at startup.

    ``classifier`` is the injected Bedrock **Haiku** guard classifier. It is
    consulted ONLY when the resolved config enables the input guard AND the
    deterministic pre-filter hits — a distinct role from ``bedrock`` (the
    generator) and from eval's judge; the no-self-grading invariant is
    untouched. Constructing it makes NO paid call.

    ``nemo`` is the injected out-of-process NeMo Guardrails pod client (HTTP).
    It is consulted ONLY when the resolved config enables the ``nemo``
    output/facts lane (``guardrails.nemo.enabled``); constructing it opens NO
    connection and makes NO paid call. ``None`` when ``ORCHESTRATOR_NEMO_GUARD_URL``
    is unset — the lane is also config-gated, so the released 1.0.0-1.4.0
    configs never reach it regardless. Per Q6b the NeMo lane is OUTPUT + FACTS
    only this slice; the input lane keeps the in-house Haiku confirm-step.

    Fields are duck-typed ``Any`` so tests install mock instances directly.
    """

    bedrock: Any
    search: Any | None = None
    classifier: Any | None = None
    nemo: Any | None = None
    # Human-readable reason search is None; surfaced by readyz and the 502.
    opensearch_unavailable_reason: str | None = None


def build_app_clients(settings: Settings) -> AppClients:
    """
    Construct the real client bundle from Settings location facts (lifespan).

    Construction-time pins (region, query-side embedder for the ``_meta``
    guard) come from the documented default acceptance config
    (:data:`app.config.DEFAULT_PIPELINE_CONFIG_REF`); per-request behavior
    pins (model ids, temperature, budgets) still arrive per call from each
    request's resolved config.

    The Bedrock generator AND the guard classifier are both constructed here
    (neither makes a paid call at construction); the guard classifier is
    injected into the input-guard stage exactly like the generator reaches the
    generation stage. The NeMo guardrail pod client is constructed from the
    ``ORCHESTRATOR_NEMO_GUARD_URL`` location fact when set (opening no
    connection); it is injected into the OUTPUT-guard stage the same way. An
    OpenSearch client that cannot be constructed NEVER crashes startup: the
    reason is recorded so ``readyz`` reports 503 and ``/query`` maps to 502
    naming the dependency.
    """
    default_config = resolve_pipeline_config(DEFAULT_PIPELINE_CONFIG_REF).config
    bedrock = BedrockClient(region=default_config.region)
    # Distinct role from the generator: the guard classifier NEVER grades the
    # generator's own output and never runs in eval. No paid call at build.
    classifier = GuardClassifier(region=default_config.region)
    # Out-of-process NeMo output/facts lane client: constructed only when the pod
    # URL location fact is set (constructing opens no connection / makes no call).
    # The lane is ALSO config-gated (guardrails.nemo), so a None here just means
    # a config that enables NeMo without a pod URL fails loudly at request time.
    nemo = (
        NemoGuardClient(base_url=settings.nemo_guard_url)
        if settings.nemo_guard_url is not None
        else None
    )

    if settings.opensearch_endpoint is None:
        return AppClients(
            bedrock=bedrock,
            search=None,
            classifier=classifier,
            nemo=nemo,
            opensearch_unavailable_reason=(
                "ORCHESTRATOR_OPENSEARCH_ENDPOINT is not set — the service "
                "has no OpenSearch domain to retrieve from."
            ),
        )

    try:
        search = OpenSearchSearchClient(
            endpoint=settings.opensearch_endpoint,
            index=settings.opensearch_index,
            embedder_model_id=default_config.embedder.model_id,
            embedder_dimensions=TITAN_EMBEDDING_DIMENSIONS,
            region=default_config.region,
        )
    except OpenSearchNotReadyError as exc:
        # _meta present-but-mismatched: not-ready, with the guard's own prose.
        return AppClients(
            bedrock=bedrock,
            search=None,
            classifier=classifier,
            nemo=nemo,
            opensearch_unavailable_reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — startup must degrade, not crash
        # The init-time mapping fetch hit a transport/credential failure;
        # record a CLEAN reason (class name only, never raw internals).
        return AppClients(
            bedrock=bedrock,
            search=None,
            classifier=classifier,
            nemo=nemo,
            opensearch_unavailable_reason=(
                f"OpenSearch was unreachable at startup ({type(exc).__name__})."
            ),
        )
    return AppClients(bedrock=bedrock, search=search, classifier=classifier, nemo=nemo)
