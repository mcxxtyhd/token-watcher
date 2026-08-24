"""Provider package: importing this registers all built-in providers."""

from .base import (
    ProviderBase,
    ProviderSnapshot,
    QuotaLevel,
    PROVIDER_REGISTRY,
    register_provider,
    build_provider,
)
from . import volcengine  # noqa: F401  registers "volcengine"
from . import minimax  # noqa: F401  registers "minimax"
from . import deepseek  # noqa: F401  registers "deepseek"
from . import qoder  # noqa: F401  registers "qoder"

__all__ = [
    "ProviderBase",
    "ProviderSnapshot",
    "QuotaLevel",
    "PROVIDER_REGISTRY",
    "register_provider",
    "build_provider",
]
