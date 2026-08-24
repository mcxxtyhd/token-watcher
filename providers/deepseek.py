"""DeepSeek balance provider.

DeepSeek is a prepaid balance model (not a plan with quotas/percentages).
`/user/balance` returns remaining CNY; there's no total/used/reset, and no
public API for spend history. We approximate "today's spend" locally by
recording the opening balance each day and diffing against the current
balance. State is kept in state.json next to config.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

from .base import ProviderBase, ProviderSnapshot, QuotaLevel, register_provider

API_URL = "https://api.deepseek.com/user/balance"


def _state_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "state.json"
    return Path(__file__).resolve().parent.parent / "state.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_path().write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


@register_provider
class DeepseekProvider(ProviderBase):
    type_name = "deepseek"

    def fetch(self) -> ProviderSnapshot:
        key = self.credentials.get("api_key", "")
        if not key:
            raise RuntimeError("缺少 API Key")
        resp = requests.get(
            API_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("is_available", False):
            raise RuntimeError("账户不可用 (is_available=false)")
        infos = data.get("balance_infos", [])
        if not infos:
            raise RuntimeError("无余额信息")
        info = infos[0]
        currency = info.get("currency", "CNY")
        total = info.get("total_balance", "0")
        symbol = "¥" if currency == "CNY" else f"{currency} "
        try:
            total_f = float(total)
        except (TypeError, ValueError):
            total_f = 0.0

        spend = self._today_spend(total_f)

        # Two levels: today's spend (default) and balance (click to cycle).
        spend_level = QuotaLevel(
            level="today_spend",
            label="今日",
            used_percent=0.0,
            reset_timestamp=None,
            display_text=f"{symbol}{spend:.2f}",
        )
        balance_level = QuotaLevel(
            level="balance",
            label="余额",
            used_percent=0.0,
            reset_timestamp=None,
            display_text=f"{symbol}{total_f:.2f}",
        )
        extra: list[str] = []
        granted = info.get("granted_balance", "0")
        topped = info.get("topped_up_balance", "0")
        extra.append(f"赠送 {symbol}{float(granted):.2f} · 充值 {symbol}{float(topped):.2f}")
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ok=True,
            error="",
            levels=[spend_level, balance_level],
            extra_lines=extra,
        )

    def _today_spend(self, current_balance: float) -> float:
        """Track opening balance per day; spend = opening - current.

        If current > opening (a top-up happened), reset opening to current
        so spend restarts from the post-top-up baseline.
        """
        state = _load_state()
        mine = state.get("deepseek", {})
        today = _today()
        opening = mine.get("opening_balance")
        last_day = mine.get("day")
        if last_day != today or opening is None:
            # New day (or first run): opening = current.
            opening = current_balance
            mine = {"day": today, "opening_balance": opening}
            state["deepseek"] = mine
            _save_state(state)
            return 0.0
        spend = opening - current_balance
        if spend < 0:
            # Topped up: rebase opening to current balance.
            mine["opening_balance"] = current_balance
            state["deepseek"] = mine
            _save_state(state)
            return 0.0
        return round(spend, 2)
