"""
Configuration loader — singleton access to the application configuration.
"""

from __future__ import annotations

from functools import lru_cache

from .schema import AppConfig


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached singleton application configuration."""
    return AppConfig()


def reload_config() -> AppConfig:
    """Force reload configuration (clears cache)."""
    get_config.cache_clear()
    return get_config()
