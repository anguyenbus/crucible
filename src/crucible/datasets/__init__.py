"""Datasets for crucible."""

from crucible.datasets.legal_rag_bench import (
    DATASET_NAME,
    DEFAULT_CACHE_DIR,
    DEFAULT_HF_TOKEN_PATH,
    HF_TOKEN_ENV,
    SLICE_NANO,
    SLICE_PICO,
    load_legal_rag_bench,
)

from crucible.datasets.gst_legal_rag import (
    DATASET_NAME as GST_DATASET_NAME,
    DEFAULT_CACHE_DIR as GST_DEFAULT_CACHE_DIR,
    SLICE_PICO as GST_SLICE_PICO,
    SLICE_NANO as GST_SLICE_NANO,
    SLICE_MINI as GST_SLICE_MINI,
    SLICE_FULL as GST_SLICE_FULL,
    load_gst_legal_rag,
)

__all__ = [
    # Legal RAG Bench exports
    "DATASET_NAME",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_HF_TOKEN_PATH",
    "HF_TOKEN_ENV",
    "SLICE_NANO",
    "SLICE_PICO",
    "load_legal_rag_bench",
    # GST Legal RAG exports
    "GST_DATASET_NAME",
    "GST_DEFAULT_CACHE_DIR",
    "GST_SLICE_PICO",
    "GST_SLICE_NANO",
    "GST_SLICE_MINI",
    "GST_SLICE_FULL",
    "load_gst_legal_rag",
]
