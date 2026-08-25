"""Provider abstraction for the coding-plan monitor.

Each provider is a self-contained class that knows how to authenticate and
fetch its own usage. The monitor talks to all providers through the same
interface, so adding a new one is just: write a subclass, register it.

Config model (config.json `providers` array entries):

    {
      "id": "volcengine",          # stable key, also shown in UI
      "type": "volcengine",         # selects the provider class
      "label": "火山方舟",          # display name
      "credentials": { ... },       # provider-specific fields
      "poll_interval_sec": 10,       # optional override
      "warning_percent": 80,
      "critical_percent": 95
    }

A "primary" provider id is stored at config top level; the ball shows that
provider's currently-selected level. Detail card lists ALL providers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QuotaLevel:
    """One quota dimension (e.g. session/weekly/monthly, or 5h/weekly)."""

    level: str  # machine key, e.g. "session", "weekly", "interval5h"
    label: str  # display label, e.g. "会话", "5h窗口"
    used_percent: float  # 0..100, already-used ratio
    reset_timestamp: int | None = None  # epoch seconds; None if unknown
    # When set, the ball shows this text instead of a percentage (e.g. "¥11.24"
    # for balance-based providers that have no total to ratio against).
    display_text: str | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    def countdown(self, now: Optional[int] = None) -> str:
        if self.reset_timestamp is None:
            return "-"
        now = now or int(time.time())
        secs = self.reset_timestamp - now
        if secs <= 0:
            return "已重置"
        d, r = divmod(secs, 86400)
        h, r = divmod(r, 3600)
        m, _ = divmod(r, 60)
        if d > 0:
            return f"{d}d {h}h"
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"


@dataclass
class ProviderSnapshot:
    """Result of one provider fetch."""

    provider_id: str
    ok: bool
    error: str  # empty string when ok
    levels: list[QuotaLevel] = field(default_factory=list)
    # Free-form extra lines to show in the detail card (e.g. model names).
    extra_lines: list[str] = field(default_factory=list)
    fetched_at: int = field(default_factory=lambda: int(time.time()))

    def get(self, level: str) -> Optional[QuotaLevel]:
        for q in self.levels:
            if q.level == level:
                return q
        return None


class ProviderBase:
    """Subclass this to add a provider.

    Implement `fetch()` to return a ProviderSnapshot. Credentials come from
    `self.credentials` (the dict from config). Raise on auth failure; the
    monitor turns exceptions into an error snapshot.
    """

    #: registry key matching config "type"
    type_name: str = ""

    def __init__(self, provider_id: str, label: str, credentials: dict, config: dict):
        self.provider_id = provider_id
        self.label = label
        self.credentials = credentials
        self.config = config  # the whole provider config entry

    def fetch(self) -> ProviderSnapshot:
        raise NotImplementedError


# Registry: type_name -> class. Populated by each provider module on import.
PROVIDER_REGISTRY: dict[str, type[ProviderBase]] = {}


def register_provider(cls: type[ProviderBase]) -> type[ProviderBase]:
    """Decorator to register a provider class by its type_name."""
    if not cls.type_name:
        raise ValueError(f"{cls.__name__} has no type_name")
    PROVIDER_REGISTRY[cls.type_name] = cls
    return cls


def build_provider(cfg_entry: dict) -> ProviderBase:
    """Factory: instantiate the right provider from a config entry."""
    ptype = cfg_entry.get("type", "")
    if ptype not in PROVIDER_REGISTRY:
        raise ValueError(f"unknown provider type: {ptype!r}")
    cls = PROVIDER_REGISTRY[ptype]
    return cls(
        provider_id=cfg_entry["id"],
        label=cfg_entry.get("label", cfg_entry["id"]),
        credentials=cfg_entry.get("credentials", {}),
        config=cfg_entry,
    )
