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

__all__ = [
    "DATASET_NAME",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_HF_TOKEN_PATH",
    "HF_TOKEN_ENV",
    "SLICE_NANO",
    "SLICE_PICO",
    "load_legal_rag_bench",
]
