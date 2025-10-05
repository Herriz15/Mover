"""Utilities for injecting a local OLAMA-backed server into a Codex installation."""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = ["CodexLocalModelIntegrator", "main"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = import_module(".tool", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:  # pragma: no cover
    from .tool import CodexLocalModelIntegrator, main
