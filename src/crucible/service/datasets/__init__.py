"""Dataset loaders for the eval service."""

from crucible.service.datasets.gst_legal_rag import (
    DATASET_NAME as GST_DATASET_NAME,
)
from crucible.service.datasets.gst_legal_rag import (
    SLICE_FULL as GST_SLICE_FULL,
)
from crucible.service.datasets.gst_legal_rag import (
    SLICE_MINI as GST_SLICE_MINI,
)
from crucible.service.datasets.gst_legal_rag import (
    SLICE_NANO as GST_SLICE_NANO,
)
from crucible.service.datasets.gst_legal_rag import (
    SLICE_PICO as GST_SLICE_PICO,
)
from crucible.service.datasets.gst_legal_rag import (
    load_gst_legal_rag,
)
from crucible.service.datasets.legal_rag_bench import (
    DATASET_NAME,
    DEFAULT_HF_TOKEN_PATH,
    HF_TOKEN_ENV,
    SLICE_NANO,
    SLICE_PICO,
    load_legal_rag_bench,
)
from crucible.service.datasets.resolve import resolve_loader

__all__ = [
    # Legal RAG Bench exports
    "DATASET_NAME",
    "DEFAULT_HF_TOKEN_PATH",
    "HF_TOKEN_ENV",
    "SLICE_NANO",
    "SLICE_PICO",
    "load_legal_rag_bench",
    # GST Legal RAG exports
    "GST_DATASET_NAME",
    "GST_SLICE_PICO",
    "GST_SLICE_NANO",
    "GST_SLICE_MINI",
    "GST_SLICE_FULL",
    "load_gst_legal_rag",
    # Routing dispatch (single source of truth)
    "resolve_loader",
]
