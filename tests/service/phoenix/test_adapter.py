"""Unit tests for ``PhoenixAdapter`` — the LIVE span-tracing path.

Rationale (see ``docs/phoenix-audit.md``): before this file, the ONLY tested
Phoenix code was ``annotations.py`` — which is production-dead (nothing in the
eval pipeline calls it). The adapter, which the runner ACTUALLY uses
(``run_golden_set`` calls ``eval_run_span`` + ``rag_query_span``), had zero
tests. This file inverts that: it covers the code that actually runs.

Two tiers:
  * Degraded-path tests run everywhere (no ``phoenix`` extra needed): they pin
    the no-op behaviour the runner relies on when Phoenix is unavailable, plus
    endpoint validation/conversion — pure logic with no tracer.
  * The live span-tree test is marked and SKIPS unless the ``phoenix`` extra is
    installed; in CI (with the extra) it asserts the real two-level
    ``eval_run -> rag_query`` tree the runner emits.
"""

from __future__ import annotations

import importlib.util

import pytest

# The adapter hard-imports openinference at module level, so the whole module
# is unimportable without the `phoenix` extra. Skip cleanly when it is absent.
pytest.importorskip(
    "openinference.semconv.trace", reason="requires the `phoenix` extra"
)

from crucible.service.phoenix.adapter import (  # noqa: E402
    PhoenixAdapter,
    suppress_tracing_if_available,
)

_HAS_OITRACER = (
    importlib.util.find_spec("opentelemetry.sdk") is not None
    and importlib.util.find_spec("openinference.instrumentation") is not None
)


def test_disabled_adapter_is_not_connected():
    """enabled=False must not build a tracer (is_connected False)."""
    adapter = PhoenixAdapter(enabled=False)
    assert adapter.is_connected() is False


def test_disabled_adapter_spans_are_noops_and_yield_ids():
    """The runner enters eval_run_span/rag_query_span even when Phoenix is off.

    Both must behave as no-op context managers that still yield string ids, so
    ``run_golden_set`` works identically with Phoenix absent.
    """
    adapter = PhoenixAdapter(enabled=False)
    with adapter.eval_run_span(run_name="t", num_questions=2) as run_id:
        assert isinstance(run_id, str) and run_id
        with adapter.rag_query_span(question="what is X?") as trace_id:
            assert isinstance(trace_id, str) and trace_id


def test_invalid_endpoint_is_rejected():
    with pytest.raises(ValueError, match="must start with http"):
        PhoenixAdapter(endpoint="localhost:6006", enabled=False)


def test_otlp_endpoint_conversion_maps_ui_port_to_grpc():
    """UI endpoint (:6006) converts to the gRPC OTLP port (:4317)."""
    adapter = PhoenixAdapter(enabled=False)
    assert adapter._get_otlp_endpoint("http://localhost:6006") == "http://localhost:4317"
    assert adapter._get_otlp_endpoint("https://phoenix.example.com:443").endswith(":4317")


def test_suppress_tracing_returns_a_context_manager():
    """The seam injected into the kernel DeepEvalEvaluator must always be a CM.

    No-op when Phoenix is absent; the real ``suppress_tracing`` when present.
    Either way it must be usable as a context manager without raising.
    """
    with suppress_tracing_if_available():
        pass


@pytest.mark.skipif(not _HAS_OITRACER, reason="requires openinference-instrumentation + otel-sdk")
def test_live_span_tree_is_eval_run_then_rag_query():
    """With a real openinference tracer, the adapter emits exactly the 2 levels.

    This is the live coverage the package never had: it proves the runner's
    span hierarchy is ``eval_run (CHAIN) -> rag_query (CHAIN)`` and nothing more
    (the retrieval/generation/evaluation child methods were removed as dead).

    The adapter's spans use ``openinference_span_kind=`` (a Phoenix/openinference
    tracer extension), so a vanilla OTel tracer cannot be used — we inject an
    ``OITracer`` backed by an in-memory exporter instead of a live gRPC tracer.
    """
    from openinference.instrumentation import OITracer, TraceConfig
    from openinference.semconv.trace import OpenInferenceSpanKindValues
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    oi_tracer = OITracer(provider.get_tracer("test"), config=TraceConfig())

    adapter = PhoenixAdapter(enabled=False)
    adapter._tracer = oi_tracer  # inject in-memory-backed openinference tracer

    with adapter.eval_run_span(run_name="unit", num_questions=1):
        with adapter.rag_query_span(question="q?"):
            pass

    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("eval_run") == 1
    assert names.count("rag_query") == 1
    # No child spans remain — they were removed as dead code.
    assert not ({"retrieval", "generation", "evaluation"} & set(names))

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    chain = OpenInferenceSpanKindValues.CHAIN.value
    for n in ("eval_run", "rag_query"):
        assert by_name[n].attributes.get("openinference.span.kind") == chain
    # rag_query is parented under eval_run.
    assert by_name["rag_query"].parent.span_id == by_name["eval_run"].context.span_id
