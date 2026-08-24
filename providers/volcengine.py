"""Volcano Engine Ark coding-plan provider.

Replays the console XHR `GetCodingPlanUsage` with the user's cookie.
We avoid requests.Session: its cookie jar merges with the manual Cookie
header and breaks the server's CSRF check. Manual header + post works.

Cookie persistence: this endpoint does NOT renew the `digest` token (only
refreshes telemetry cookies), so there's nothing useful to write back.
Credential updates happen via the settings dialog (paste new cURL).
"""

from __future__ import annotations

import requests

from .base import ProviderBase, ProviderSnapshot, QuotaLevel, register_provider

API_URL = (
    "https://console.volcengine.com/api/top/ark/cn-beijing/2024-01-01/GetCodingPlanUsage"
)
REFERER = (
    "https://console.volcengine.com/ark/region:cn-beijing/subscription/coding-plan"
)
ORIGIN = "https://console.volcengine.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Volcano has three quota levels; map API names to display labels.
LEVEL_DEFS = [
    ("session", "5h"),
    ("weekly", "周"),
    ("monthly", "月"),
]


@register_provider
class VolcengineProvider(ProviderBase):
    type_name = "volcengine"

    def _headers(self) -> dict[str, str]:
        c = self.credentials
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh",
            "content-type": "application/json",
            "origin": ORIGIN,
            "referer": REFERER,
            "user-agent": USER_AGENT,
            "x-csrf-token": c.get("csrf_token", ""),
            "x-web-id": c.get("x_web_id", ""),
            "Cookie": c.get("cookie", ""),
        }

    def fetch(self) -> ProviderSnapshot:
        resp = requests.post(API_URL, data="{}", headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("ResponseMetadata", {})
        if "Error" in meta:
            err = meta["Error"]
            raise RuntimeError(f"{err.get('Code')}: {err.get('Message')}")
        result = data["Result"]
        by_level = {item["Level"]: item for item in result["QuotaUsage"]}
        levels: list[QuotaLevel] = []
        for key, label in LEVEL_DEFS:
            item = by_level.get(key)
            if not item:
                continue
            levels.append(
                QuotaLevel(
                    level=key,
                    label=label,
                    used_percent=item["Percent"],
                    reset_timestamp=item["ResetTimestamp"],
                )
            )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ok=True,
            error="",
            levels=levels,
        )
