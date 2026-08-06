"""Stable foundations for the AnyAICam modular application.

This package is intentionally dependency-free so modules can be extracted from
``app.main`` incrementally without changing the production entry point.
"""

from .module_registry import MODULES, ModuleBoundary, validate_module_registry

__all__ = ["MODULES", "ModuleBoundary", "validate_module_registry"]
