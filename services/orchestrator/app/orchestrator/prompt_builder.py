"""
Prompt construction stage: render the config-inlined template.

Deterministic PURE function. The template TEXT arrives from the resolved
pinned config (it is inlined in the config YAML and covered by
``config_sha256``) — this stage never reads files or the environment. The
optional ``{history}`` placeholder (Phase B, ``legal-rag-default-1.2.0``)
renders a config-pinned window of FULL prior turns; templates without the
placeholder (1.0.0/1.1.0) are unaffected.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.pipeline_config import HistoryPins
    from app.schemas.query import HistoryTurn

_ROLE_LABELS: Final[dict[str, str]] = {"user": "User", "assistant": "Assistant"}
_HISTORY_HEADER: Final[str] = "Conversation so far:"

# The three template placeholders, matched in ONE pass so substituted values
# are never re-scanned (a reserved token like ``{context}`` appearing inside
# history or a retrieved chunk must survive verbatim — see ``build_prompt``).
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{history\}|\{context\}|\{question\}")


def _render_history_block(history: Sequence[HistoryTurn] | None, pins: HistoryPins | None) -> str:
    """
    Render the ``{history}`` block, or ``""`` when there is nothing to render.

    A config-pinned window of recent turns (``prompt_window_turns``, user AND
    assistant) is selected newest-first under ``prompt_char_budget`` (whole-turn
    tail truncation: the oldest turns fall out first and the kept turns are a
    contiguous newest suffix; the budget counts rendered ``Role: text`` line
    characters). The kept turns render chronologically as ``User:``/
    ``Assistant:`` lines in a delimited section that ENDS in a blank line, so
    the template's following section starts flush-left. Empty selection → an
    empty string: the rendered prompt is then equivalent to the pre-history
    (1.1.0) prompt.
    """
    if not history or pins is None:
        return ""

    selected: list[HistoryTurn] = []
    used_chars = 0
    for turn in reversed(history):
        if len(selected) >= pins.prompt_window_turns:
            break
        line_chars = len(_ROLE_LABELS[turn.role]) + len(": ") + len(turn.text)
        if used_chars + line_chars > pins.prompt_char_budget:
            break
        selected.append(turn)
        used_chars += line_chars

    if not selected:
        return ""
    selected.reverse()
    lines = [f"{_ROLE_LABELS[turn.role]}: {turn.text}" for turn in selected]
    return _HISTORY_HEADER + "\n" + "\n".join(lines) + "\n\n"


def build_prompt(
    template_text: str,
    *,
    context: str,
    question: str,
    history: Sequence[HistoryTurn] | None = None,
    pins: HistoryPins | None = None,
) -> str:
    """
    Render the prompt from the config-carried template text.

    Rendering is a SINGLE-PASS placeholder substitution (NOT ``str.format``,
    NOT chained ``str.replace``) so literal braces in the template — e.g. the
    ``[doc_id:chunk_idx]`` citation-marker instruction — AND any reserved token
    (``{history}``/``{context}``/``{question}``) that appears inside the
    substituted history, context, or question text survive verbatim. A chained
    ``.replace`` would re-scan already-inserted values and, for example, splice
    the retrieved context into a history turn that merely quoted ``{context}``;
    matching every placeholder in one regex pass over the TEMPLATE avoids that.

    Args:
        template_text: Template text from ``config.prompt_template.text``.
        context: Assembled ``[chunk_id]: text`` context blocks.
        question: The CURRENT user question — never the history-prefixed
            retrieval query (history reaches the prompt only via the
            ``{history}`` block).
        history: Prior turns, oldest first, for the ``{history}`` block.
        pins: The resolved config's ``history`` pins block.

    Returns:
        The fully rendered prompt string.

    """
    substitutions = {
        "{history}": _render_history_block(history, pins),
        "{context}": context,
        "{question}": question,
    }
    return _PLACEHOLDER_RE.sub(lambda match: substitutions[match.group(0)], template_text)
