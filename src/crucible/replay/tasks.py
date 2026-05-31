"""
Task wrappers for Phoenix experiment execution.

This module provides task wrappers for candidate and baseline adapters
in Phoenix replay evaluation.
"""

from __future__ import annotations

from collections.abc import Callable

from beartype import beartype
from beartype.typing import Any


@beartype
class CandidateTask:
    """
    Wrapper for candidate adapter invocation.

    Wraps an adapter function for execution within Phoenix experiments.

    Attributes:
        _adapter: Adapter function to wrap.
        _name: Task name.

    Example:
        >>> task = CandidateTask(my_adapter, "candidate_v1")
        >>> result = task(input_data)

    """

    __slots__ = ("_adapter", "_name")

    def __init__(self, adapter: Callable, name: str) -> None:
        """
        Initialize candidate task.

        Args:
            adapter: Adapter function to wrap.
            name: Task name.

        """
        self._adapter = adapter
        self._name = name

    def __call__(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute candidate task.

        Args:
            input_data: Input data for the adapter.

        Returns:
            Structured output dictionary.

        """
        try:
            result = self._adapter(input_data)
            return {
                "success": True,
                "output": result,
                "error": None,
                "task_name": self._name,
            }
        except Exception as e:
            return {
                "success": False,
                "output": None,
                "error": str(e),
                "task_name": self._name,
            }


@beartype
class BaselineTask:
    """
    Wrapper for baseline adapter invocation.

    Wraps a baseline adapter function for execution within Phoenix experiments.

    Attributes:
        _adapter: Adapter function to wrap.
        _name: Task name.

    Example:
        >>> task = BaselineTask(baseline_adapter, "baseline_v1")
        >>> result = task(input_data)

    """

    __slots__ = ("_adapter", "_name")

    def __init__(self, adapter: Callable, name: str) -> None:
        """
        Initialize baseline task.

        Args:
            adapter: Adapter function to wrap.
            name: Task name.

        """
        self._adapter = adapter
        self._name = name

    def __call__(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute baseline task.

        Args:
            input_data: Input data for the adapter.

        Returns:
            Structured output dictionary.

        """
        try:
            result = self._adapter(input_data)
            return {
                "success": True,
                "output": result,
                "error": None,
                "task_name": self._name,
            }
        except Exception as e:
            return {
                "success": False,
                "output": None,
                "error": str(e),
                "task_name": self._name,
            }
