"""MiniMax Token Plan coding-plan provider.

Fetches `coding_plan/remains` with just an API Key (no cookie, no CSRF).
The response is per-model; for plans that aren't count-based (total=0),
the meaningful field is `remaining_percent`, not the count fields.
"""

from __future__ import annotations

import requests

from .base import ProviderBase, ProviderSnapshot, QuotaLevel, register_provider

API_URL = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"

# MiniMax exposes two time windows. The "primary" model for coding is
# "general"; other models (e.g. "video") are surfaced as extra lines.
PRIMARY_MODEL = "general"


@register_provider
class MinimaxProvider(ProviderBase):
    type_name = "minimax"

    def fetch(self) -> ProviderSnapshot:
        key = self.credentials.get("api_key", "")
        if not key:
            raise RuntimeError("缺少 API Key")
        resp = requests.get(
            API_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("base_resp", {}).get("status_code")
        if status != 0:
            msg = data.get("base_resp", {}).get("status_msg", "查询失败")
            if status == 1004:
                raise RuntimeError("API Key 无效")
            raise RuntimeError(msg)

        models = data.get("model_remains", [])
        primary = next((m for m in models if m.get("model_name") == PRIMARY_MODEL), None)
        if primary is None and models:
            primary = models[0]

        levels: list[QuotaLevel] = []
        extra_lines: list[str] = []

        if primary:
            # 5h interval window.
            interval_remain = primary.get("current_interval_remaining_percent")
            if isinstance(interval_remain, (int, float)):
                levels.append(
                    QuotaLevel(
                        level="interval5h",
                        label="5h窗口",
                        used_percent=100.0 - interval_remain,
                        reset_timestamp=_ms_to_s(primary.get("remains_time")),
                    )
                )
            # Weekly window.
            weekly_remain = primary.get("current_weekly_remaining_percent")
            if isinstance(weekly_remain, (int, float)):
                levels.append(
                    QuotaLevel(
                        level="weekly",
                        label="周",
                        used_percent=100.0 - weekly_remain,
                        reset_timestamp=_ms_to_s(primary.get("weekly_remains_time")),
                    )
                )

        # Other models as extra info lines.
        for m in models:
            name = m.get("model_name", "?")
            if name == (primary.get("model_name") if primary else None):
                continue
            r = m.get("current_interval_remaining_percent")
            extra_lines.append(
                f"{name}: 剩 {r}%" if isinstance(r, (int, float)) else f"{name}: -"
            )

        return ProviderSnapshot(
            provider_id=self.provider_id,
            ok=True,
            error="",
            levels=levels,
            extra_lines=extra_lines,
        )


def _ms_to_s(remains_time_ms) -> int | None:
    """`remains_time` is milliseconds-from-now to reset; convert to epoch s."""
    if not isinstance(remains_time_ms, (int, float)):
        return None
    import time
    return int(time.time() + remains_time_ms / 1000)
