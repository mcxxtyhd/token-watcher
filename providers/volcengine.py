"""Volcano Engine Ark coding-plan provider.

Runs the GetCodingPlanUsage quota API via a dedicated Chrome + CDP, mirroring
the Qoder pattern. The fetch happens inside the page origin (Runtime.evaluate)
so the browser handles cookies and the x-csrf-token / x-web-id headers
automatically -- we never have to scrape them off intercepted requests.

The dedicated Chrome owns the session cookie in its profile dir; first login
(and re-login every ~48 h when the cookie expires) happens in a visible
window opened by the settings dialog.
"""

from __future__ import annotations

import logging

from .base import ProviderBase, ProviderSnapshot, QuotaLevel, register_provider
from .volcengine_cdp import (
    DEFAULT_PORT,
    LOGIN_PORT,
    LoginWindowOpen,
    ProfileBusy,
    VolcengineCdp,
    close_browser,
    profile_op,
    session_is_valid,
)

_log = logging.getLogger("volcengine")
if not _log.handlers:
    _log.setLevel(logging.DEBUG)
    import sys
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(
        logging.Formatter("[volc.fetch] %(asctime)s.%(msecs)03d "
                          "%(levelname)-5s %(message)s",
                          datefmt="%H:%M:%S")
    )
    _log.addHandler(_h)
_log.propagate = False

# Volcano has three quota levels; map API names to display labels.
LEVEL_DEFS = [
    ("session", "5h"),
    ("weekly", "周"),
    ("monthly", "月"),
]


@register_provider
class VolcengineProvider(ProviderBase):
    type_name = "volcengine"

    def fetch(self) -> ProviderSnapshot:
        port = int(self.credentials.get("cdp_port", DEFAULT_PORT))
        _log.info(f"fetch start: port={port}")
        try:
            return self._fetch_locked(port)
        except ProfileBusy as e:
            # open_login_window (button click) or another profile operation
            # is mid-flight; spawning headless now would race it for the
            # Chrome singleton. Skip the cycle, keep the last snapshot.
            _log.info(f"fetch deferred: {e}")
            raise LoginWindowOpen("火山方舟浏览器操作进行中") from e

    def parse_result(self, res: dict | None) -> ProviderSnapshot:
        """Convert a raw fetch result ({status, body}) into a ProviderSnapshot.

        Extracted from _fetch_locked so the login probe can pass through the
        data it just verified in the login window's tab, avoiding a
        redundant fetch on the headless Chrome (saves ~2-3 s off the
        ball-update latency after the user closes the browser).
        """
        status = res.get("status") if res else None
        body = res.get("body") if res else None
        if not isinstance(body, dict):
            body = {}
        _log.info(f"fetch response: status={status} body_keys="
                  f"{list(body.keys()) if isinstance(body, dict) else type(body).__name__}")
        if status == -1:
            raise RuntimeError(f"火山方舟请求失败: {body.get('error', '未知错误')}")
        if status == 401:
            raise RuntimeError("火山方舟登录已过期,请在设置中重新登录")
        if status != 200:
            raise RuntimeError(f"火山方舟 HTTP {status}: {body}")

        meta = body.get("ResponseMetadata", {})
        if "Error" in meta:
            err = meta["Error"]
            raise RuntimeError(f"{err.get('Code')}: {err.get('Message')}")
        result = body["Result"]
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
        if not levels:
            raise RuntimeError("火山方舟未返回配额数据")
        _log.info(f"fetch ok: {[f'{l.level}={l.used_percent:.1f}%' for l in levels]}")
        return ProviderSnapshot(
            provider_id=self.provider_id,
            ok=True,
            error="",
            levels=levels,
        )

    def _fetch_locked(self, port: int) -> ProviderSnapshot:
        # The whole close-window / spawn-headless / fetch sequence must be
        # atomic with respect to open_login_window(): Chrome's process
        # singleton means overlapping spawn sequences kill one of the two
        # Chromes (debug port never opens -> 15 s timeout -> red ball).
        with profile_op():
            # The visible login window (LOGIN_PORT) holds the profile's
            # singleton lock: spawning headless Chrome on the same
            # user-data-dir while it runs would forward to the existing
            # process and time out after 10s. We only close it when the
            # session is REALLY valid (verify_session does a live API
            # call) -- stale cookies linger in the profile after expiry,
            # and closing the window on them would trap the user out of
            # re-logging-in.
            login_cdp = VolcengineCdp(LOGIN_PORT)
            if login_cdp.is_alive(timeout=0.5):
                res = login_cdp.verify_session()
                if session_is_valid(res):
                    _log.info("login window has a live session; "
                              "closing it to free the profile lock")
                    if not close_browser(login_cdp):
                        # Window didn't die -- spawning headless would just
                        # forward to the zombie and time out. Skip this
                        # cycle.
                        raise LoginWindowOpen("登录窗口未能自动关闭")
                else:
                    _log.info(f"login window open, session not valid yet "
                              f"(status={res and res.get('status')}); skipping cycle")
                    raise LoginWindowOpen("火山方舟登录窗口打开中")
            cdp = VolcengineCdp(port)
            # Headless Chrome on the same profile as the visible login
            # window: the session cookie persists across visible /
            # headless mode.
            cdp.spawn(headless=True)
            results = cdp.eval_fetch()
        if not results:
            _log.error("fetch returned empty results array")
            raise RuntimeError("火山方舟返回为空")
        return self.parse_result(results[0])