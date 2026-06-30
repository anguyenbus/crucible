"""
Single routing-dispatch point for dataset slice selection.

Phase 3 consolidates the FIVE ``slice_name.startswith("gst_")`` routing/dispatch
sites that previously lived inline in ``experiments/datasets.py`` (x2) and
``runners/run_rag_eval.py`` (x3) into ONE place. All callers route dataset
selection through ``resolve_loader``; no other ``startswith("gst_")`` routing or
dispatch remains.

NOTE: the ``not slice_name.startswith("gst_")`` check inside
``gst_legal_rag._get_gst_slice_limit`` is a defensive VALIDATION guard (it
rejects a non-``gst_`` slice handed to the GST loader), NOT a routing decision;
it is intentionally left in place and is not folded into this resolver.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from beartype import beartype
from beartype.typing import Callable

from crucible.service.datasets.gst_legal_rag import load_gst_legal_rag
from crucible.service.datasets.legal_rag_bench import load_legal_rag_bench

# Routing keys/names per dataset family. The GST family is selected by the
# ``gst_`` slice prefix; everything else routes to Legal RAG Bench.
_GST_PREFIX: Final[str] = "gst_"

_GST_CONFIG_KEY: Final[str] = "gst_legal_rag"
_GST_DATASET_NAME: Final[str] = "gst-legal-rag"
_GST_DISPLAY_NAME: Final[str] = "GST Legal RAG"

_LRB_CONFIG_KEY: Final[str] = "legal_rag_bench"
_LRB_DATASET_NAME: Final[str] = "legal-rag-bench"
_LRB_DISPLAY_NAME: Final[str] = "Legal RAG Bench"

# Loader signature: (cache_dir, slice, force_refresh) -> iterator of query tuples.
DatasetLoader = Callable[..., Iterator[tuple[str, str, str, str]]]


@dataclass(frozen=True, slots=True)
class DatasetRouting:
    """
    Resolved routing facts for a dataset slice.

    Attributes:
        loader: The dataset loader function for this slice's family.
        config_key: The ``config["datasets"][...]`` key for this family.
        dataset_name: The hyphenated base dataset name (Phoenix dataset prefix).
        display_name: A human-readable family name for log messages.

    """

    loader: DatasetLoader
    config_key: str
    dataset_name: str
    display_name: str


@beartype
def is_gst_slice(slice_name: str) -> bool:
    """
    Return whether ``slice_name`` routes to the GST Legal RAG family.

    This is the SINGLE place the ``gst_`` routing prefix is interpreted.

    Args:
        slice_name: Dataset slice (e.g. "pico", "nano", "gst_pico", "gst_full").

    Returns:
        True if the slice routes to the GST loader, False otherwise.

    """
    return slice_name.startswith(_GST_PREFIX)


@beartype
def resolve_routing(slice_name: str) -> DatasetRouting:
    """
    Resolve all routing facts (loader + names) for a dataset slice.

    Args:
        slice_name: Dataset slice (e.g. "pico", "nano", "gst_pico", "gst_full").

    Returns:
        A ``DatasetRouting`` with the loader and the family's config key + names.

    """
    if is_gst_slice(slice_name):
        return DatasetRouting(
            loader=load_gst_legal_rag,
            config_key=_GST_CONFIG_KEY,
            dataset_name=_GST_DATASET_NAME,
            display_name=_GST_DISPLAY_NAME,
        )
    return DatasetRouting(
        loader=load_legal_rag_bench,
        config_key=_LRB_CONFIG_KEY,
        dataset_name=_LRB_DATASET_NAME,
        display_name=_LRB_DISPLAY_NAME,
    )


@beartype
def resolve_loader(slice_name: str) -> DatasetLoader:
    """
    Resolve just the loader function for a dataset slice.

    Convenience wrapper over ``resolve_routing`` for callers that only need the
    loader.

    Args:
        slice_name: Dataset slice (e.g. "pico", "nano", "gst_pico", "gst_full").

    Returns:
        The dataset loader function for this slice's family.

    """
    return resolve_routing(slice_name).loader
