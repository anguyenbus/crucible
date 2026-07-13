"""
Chainlit chat UI over the orchestrator's LIVE RAG pipeline (thin callbacks).

Run: ``uv run chainlit run app.py`` (see README.md). All testable logic lives
in ``chat_ui/`` (SSE parsing, history windowing, envelope→render mapping,
tracing) — this file only wires Chainlit callbacks to those modules over
HTTP. It NEVER imports the orchestrator's ``app.*`` package.

Honesty rules enforced here:
- Startup gate: ``GET /readyz`` must return 200; otherwise the REAL
  dependency error is shown and the chat stops — no degraded fake mode.
- Tokens render live via ``msg.stream_token()`` per SSE ``token`` event —
  real deltas only, no client-side pacing or fake progress.
- Citations/sources/timings/provenance render ONLY after the ``final`` event.
- An ``error`` event renders typed; partial streamed text stays visible but
  is marked INCOMPLETE and the turn is NOT appended to session history.
"""

from __future__ import annotations

import atexit

import chainlit as cl
from chat_elements import numbered_source_elements
from chat_ui import client, config, history, render, tracing

# One tracer for the process (pattern-copied from the service's lifespan
# bootstrap). PHOENIX_ENDPOINT unset → genuine no-op tracer; UI fully works.
TRACER, _TRACER_PROVIDER = tracing.configure_tracer(config.phoenix_endpoint())

# Flush the span batch on process exit so the LAST chat turn's CHAIN span (and
# its OUTPUT_VALUE) is exported — the service flushes in its lifespan teardown;
# the UI has no equivalent, so BatchSpanProcessor could otherwise drop spans
# still in the batch window. No-op under the no-op tracer (provider is None).
if _TRACER_PROVIDER is not None:
    atexit.register(_TRACER_PROVIDER.shutdown)

# Phoenix routes project pages by GID, not name — resolve it ONCE at startup so
# rendered trace links resolve. Best-effort: None (→ base-URL fallback) when
# Phoenix is unset/unreachable at boot; the UI stays fully functional.
PHOENIX_PROJECT_GID = client.resolve_phoenix_project_gid(
    config.phoenix_endpoint(), render.PHOENIX_PROJECT
)


def _welcome_text() -> str:
    """Welcome message: pinned config ref, per-turn cost, session-only memory."""
    return (
        f"Connected to the LIVE RAG pipeline at `{config.orchestrator_url()}`.\n\n"
        f"- **Pinned config**: `{config.pipeline_config_ref()}` (every behavior pin, "
        "including the history windows and prompt template, is covered by its "
        "`config_sha256`).\n"
        "- **Cost**: each turn makes exactly ONE paid Bedrock generation plus one "
        "Titan query embedding. The follow-up query rewrite is deterministic — it "
        "adds no paid call.\n"
        "- **Memory**: conversation history is PER-SESSION only (kept in this "
        "browser session, sent with each request); nothing persists across sessions."
    )


async def _gate_ready() -> bool:
    """Probe /readyz; on failure show the REAL error and stop (no fake mode)."""
    ready, detail = await client.check_ready(config.orchestrator_url())
    cl.user_session.set("ready", ready)
    if not ready:
        await cl.Message(
            content=(
                "**Orchestrator is NOT ready — this chat cannot run.**\n\n"
                f"Real error: {detail}\n\n"
                "There is no fallback mode. Fix the dependency above (see the "
                "README for bringing the stack up), then send a message to re-check."
            )
        ).send()
    return ready


@cl.on_chat_start
async def on_chat_start() -> None:
    """Startup gate (readyz) + honest welcome banner + empty session history."""
    cl.user_session.set("history", [])
    if await _gate_ready():
        await cl.Message(content=_welcome_text()).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """One chat turn: stream tokens live, then render the final envelope."""
    if not cl.user_session.get("ready") and not await _gate_ready():
        return

    session_history: list[dict[str, str]] = cl.user_session.get("history") or []
    payload: dict[str, object] = {
        "question": message.content,
        "pipeline_config": config.pipeline_config_ref(),
    }
    if session_history:
        payload["history"] = history.bound_history(session_history)

    answer_msg = cl.Message(content="")
    outcome: render.FinalRender | render.ErrorRender | None = None
    streamed_any = False

    with tracing.chat_turn_span(TRACER, question=message.content) as turn_span:
        # traceparent from the CHAIN span joins the pipeline root to this
        # trace; empty under the no-op tracer (nothing worth injecting).
        headers = tracing.inject_trace_headers()
        try:
            async for event in client.stream_query(config.orchestrator_url(), payload, headers):
                if event.event == "token":
                    # Real deltas only — no client-side typewriter pacing. Raw
                    # [chunk_id] markers stream as-is; the final branch swaps
                    # them for [n] (show-then-swap, T3).
                    await answer_msg.stream_token(event.data["text"])
                    streamed_any = True
                elif event.event == "final":
                    outcome = render.map_final_envelope(
                        event.data,
                        phoenix_endpoint=config.phoenix_endpoint(),
                        project_gid=PHOENIX_PROJECT_GID,
                    )
                    break
                elif event.event == "error":
                    outcome = render.map_error_event(event.data)
                    break
        except client.QueryHTTPError as error:
            # Pre-stream failure: the orchestrator's typed HTTP error, verbatim.
            outcome = render.ErrorRender(detail=error.detail, http_equivalent=error.status_code)
        except Exception as error:  # noqa: BLE001 — transport failure IS the signal
            outcome = render.ErrorRender(
                detail=f"stream transport failed ({type(error).__name__}: {error})"
            )

        if isinstance(outcome, render.FinalRender):
            tracing.set_turn_output(turn_span, outcome.answer_text)

    if isinstance(outcome, render.FinalRender):
        # Show-then-swap (T3): the streamed content is the raw [chunk_id] text;
        # overwrite it with the numbered [n] form and attach one clickable side
        # element per cited chunk to THIS message (elements are for_id-scoped, so
        # they must live on the answer to be clickable where the user reads —
        # NOT on the details block). number_citations() derives everything from
        # the final envelope, so this also covers the not-streamed_any edge:
        # the answer shows the envelope text, now numbered. Zero citations →
        # numbered.text == answer_text unchanged and no elements (honest, quiet).
        numbered = render.number_citations(outcome)
        answer_msg.content = numbered.text
        answer_msg.elements = numbered_source_elements(numbered)
        # send() ends the stream, re-emits the (now numbered) content, and
        # attaches the elements for_id=answer_msg — the documented finalize for
        # a streamed message in chainlit 2.11.1.
        await answer_msg.send()
        # Details block keeps timings/provenance/Phoenix link and now cross-
        # references the SAME [n] numbers; the clickable elements moved to the
        # answer, so it no longer carries any.
        await cl.Message(content=render.format_final_details(outcome, numbered)).send()
    elif isinstance(outcome, render.ErrorRender):
        if streamed_any:
            # Partial text stays visible but is clearly marked INCOMPLETE.
            answer_msg.content += "\n\n--- INCOMPLETE (the stream failed; see below) ---"
            await answer_msg.send()
        await cl.Message(content=render.format_error(outcome, had_partial_text=streamed_any)).send()
    else:
        # A stream that closed with no terminal event violates the contract;
        # say so rather than pretending the answer completed.
        if streamed_any:
            await answer_msg.send()
        await cl.Message(
            content=(
                "The stream ended WITHOUT a terminal event (contract violation) — "
                "treat any text above as INCOMPLETE. This turn was not saved."
            )
        ).send()

    # History: appended ONLY after a completed turn (final envelope).
    cl.user_session.set(
        "history", history.history_after_turn(session_history, message.content, outcome)
    )
