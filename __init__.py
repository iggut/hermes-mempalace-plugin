"""MemPalace Memory Plugin — thin Hermes lifecycle adapter.

Public API:
    load_plugin()   — returns this module (for Hermes plugin system)
    load_config()   — from config.py
    MemPalaceConfig — from config.py
    MemPalaceAPI    — from api.py
    MemPalaceMemoryProvider — from provider.py

All feature flags default to off. Retrieval is on by default.
Memory provider activates with ``memory.provider: mempalace`` in config.
"""

from __future__ import annotations

from .config import MemPalaceConfig, load_config
from .api import MemPalaceAPI
from .provider import MemPalaceMemoryProvider
from .retrieval import MemPalaceRetrieval

__all__ = [
    "load_config",
    "load_memory_provider",
    "MemPalaceConfig",
    "MemPalaceAPI",
    "MemPalaceMemoryProvider",
    "MemPalaceRetrieval",
    "register",
]


def load_memory_provider(config_data=None):
    """Return a configured MemPalaceMemoryProvider for Hermes memory loading."""
    cfg = load_config(config_data) if config_data is not None else load_config()
    return MemPalaceMemoryProvider(cfg)


def register(ctx) -> None:
    """Hermes memory plugin entry: register MemPalaceMemoryProvider."""
    ctx.register_memory_provider(load_memory_provider())


def load_plugin():
    """Return this module for the Hermes plugin loader."""
    import sys
    from pathlib import Path
    spec = __spec__
    if spec and spec.loader:
        return spec.loader.load_module()
    # Fallback: re-import from file
    import importlib.util
    plugin_path = Path(__file__).resolve()
    spec = importlib.util.spec_from_file_location("mempalace_plugin", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod