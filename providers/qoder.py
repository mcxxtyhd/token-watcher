"""Qoder Credits provider (阿里通义 Agentic Coding 平台).

Qoder has no api_key mechanism for usage queries: the two quota endpoints
require the browser-encrypted `qoder_session_cookie`, which cannot be
replayed from Python (bound to browser fingerprint; curl gets 401).

Approach: a dedicated Chrome instance that we own.
- spawn `chrome.exe --remote-debugging-port=<port> --user-data-dir=<profile>`
  (Chrome 136+ forbids debugging on the default profile, so the profile dir
  MUST be a dedicated one next to config.json)
- talk raw CDP over websocket (only `Runtime.evaluate` is needed; no
  Playwright -- keeps the packaged exe small)
- evaluate a fetch() in the qoder.com page origin so cookies ride along

First login (and re-login every ~8 days when the session cookie expires)
happens in a visible window opened by `open_login_window()`; the cookie
persists in the dedicated profile.

Endpoints:
- /api/v2/me/usages/big_model_credits             -> plan_quota + nextResetAt(ms)
- /api/v1/me/organization-shared-usages/big_model_credits -> shared_quota
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

from .base import ProviderBase, ProviderSnapshot, QuotaLevel, register_provider

V2_API = "https://qoder.com/api/v2/me/usages/big_model_credits"
V1_API = "https://qoder.com/api/v1/me/organization-shared-usages/big_model_credits"

DEFAULT_PORT = 9333

# JS executed via CDP: wait until the tab has landed on qoder.com (target
# creation is racy), then fetch both endpoints and return status+body.
_EVAL_FETCH = """
(async () => {
  const urls = ["__V2__", "__V1__"];
  const t0 = Date.now();
  while (!location.href.startsWith("https://qoder.com") && Date.now() - t0 < 15000) {
    await new Promise(r => setTimeout(r, 200));
  }
  if (!location.href.startsWith("https://qoder.com")) {
    return [{status: -1, body: {error: "navigation timeout"}}];
  }
  const out = [];
  for (const u of urls) {
    try {
      const r = await fetch(u, {credentials: "include",
        headers: {"bx-v": "2.5.35", "accept": "application/json"}});
      out.push({status: r.status, body: await r.json()});
    } catch (e) {
      out.push({status: -1, body: {error: String(e)}});
    }
  }
  return out;
})()
""".replace("__V2__", V2_API).replace("__V1__", V1_API)

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _config_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _find_browser() -> str | None:
    import shutil

    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in _CHROME_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


class QoderCdp:
    """Minimal CDP client: one page target + Runtime.evaluate."""

    def __init__(self, port: int):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def is_alive(self, timeout: float = 2) -> bool:
        # ``timeout`` param keeps the signature compatible with
        # volcengine_cdp.close_browser, which polls this with a short
        # timeout (a refused loopback connection can take seconds to raise
        # on machines where security software intercepts loopback traffic).
        try:
            r = requests.get(f"{self.base}/json/version", timeout=timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def has_session_cookie(self) -> bool:
        """Non-intrusive login check: ask the existing browser (no new tab,
        no navigation) whether the qoder session cookie is present.

        Called from the settings dialog's poll loop. The whole point of
        avoiding `fetch()` here is to NOT steal focus from the user's login
        window -- opening a new tab or navigating to qoder.com both pop the
        Chrome window to the foreground on Windows.
        """
        try:
            tabs = requests.get(f"{self.base}/json/list", timeout=3).json()
        except requests.RequestException:
            return False
        if not tabs:
            return False
        # Reuse the user's existing tab (usually qoder.com/account/usage)
        # instead of /json/new'ing a new one. About:blank is fine for the
        # cookie query -- Network.getCookies is origin-scoped, not page-scoped.
        target = next((t for t in tabs if t.get("type") == "page"), tabs[0])
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        try:
            from websockets.sync.client import connect
        except ImportError:
            return False
        try:
            with connect(ws_url, open_timeout=5, close_timeout=3) as ws:
                self._send(ws, 1, "Network.enable")
                self._send(
                    ws, 2, "Network.getCookies",
                    urls=["https://qoder.com"],
                )
                deadline = time.time() + 5
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=5))
                    if msg.get("id") == 2:
                        cookies = msg.get("result", {}).get("cookies", [])
                        return any(
                            c.get("name") == "qoder_session_cookie"
                            for c in cookies
                        )
        except Exception:
            return False
        return False

    def spawn(self, headless: bool = True) -> None:
        """Start a dedicated Chrome with the debug port. No-op if alive."""
        if self.is_alive():
            return
        exe = _find_browser()
        if not exe:
            raise RuntimeError("未找到 Chrome/Edge，请安装 Chrome 后重试")
        args = [
            exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={_profile_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            args.append("--headless=new")
        args.append("about:blank")
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if headless else 0,
        )
        for _ in range(50):  # wait up to ~10s for the debug port
            if self.is_alive():
                return
            time.sleep(0.2)
        raise RuntimeError("Chrome 调试端口启动超时")

    def eval_fetch(self) -> list[dict]:
        """Open a tab on qoder.com, evaluate the fetch, return results.

        The tab is created on about:blank and navigated explicitly; we then
        poll `location.href` until the qoder.com execution context is live.
        Evaluating during navigation fails with "execution context destroyed"
        (each poll is a fresh short evaluate, so a mid-navigation swap just
        fails that one poll and retries) -- running the fetch promise blindly
        right after /json/new races the navigation and loses.
        """
        # Newer Chrome requires PUT for /json/new.
        r = requests.put(f"{self.base}/json/new?about:blank", timeout=10)
        if r.status_code != 200:
            r = requests.get(f"{self.base}/json/new?about:blank", timeout=10)
        r.raise_for_status()
        target = r.json()
        ws_url = target["webSocketDebuggerUrl"]
        target_id = target["id"]
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise RuntimeError("缺少 websockets 依赖 (pip install websockets)") from e

        try:
            with connect(ws_url, open_timeout=10, close_timeout=5) as ws:
                self._send(ws, 1, "Runtime.enable")
                self._send(ws, 2, "Page.enable")
                self._send(ws, 3, "Page.navigate", url="https://qoder.com/")
                msg_id = 4
                deadline = time.time() + 30
                ready = False
                while time.time() < deadline and not ready:
                    self._send(
                        ws, msg_id, "Runtime.evaluate",
                        expression="location.href", returnByValue=True,
                    )
                    reply = self._recv_for(ws, msg_id)
                    # CDP evaluate replies nest: {result: {result: RemoteObject}}.
                    res = ((reply or {}).get("result") or {}).get("result", {})
                    if res.get("type") == "string" and res.get("value", "").startswith("https://qoder.com"):
                        ready = True
                    else:
                        time.sleep(0.3)
                        msg_id += 1
                if not ready:
                    raise RuntimeError("qoder.com 页面加载超时")
                msg_id += 1
                self._send(
                    ws,
                    msg_id,
                    "Runtime.evaluate",
                    expression=_EVAL_FETCH,
                    awaitPromise=True,
                    returnByValue=True,
                )
                # Skip replies until our evaluate id comes back.
                deadline = time.time() + 40
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=40))
                    if msg.get("id") == msg_id:
                        result = msg.get("result", {}).get("result", {})
                        if result.get("type") == "object" and "value" in result:
                            return result["value"]
                        raise RuntimeError(f"CDP 返回异常: {msg.get('result') or msg.get('error')}")
        finally:
            try:
                requests.get(f"{self.base}/json/close/{target_id}", timeout=5)
            except requests.RequestException:
                pass
        raise RuntimeError("CDP 求值超时")

    @staticmethod
    def _send(ws, msg_id: int, method: str, **params) -> None:
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))

    @staticmethod
    def _recv_for(ws, msg_id: int, timeout: float = 10) -> dict | None:
        """Read messages until the reply for `msg_id` arrives.

        Returns None instead of raising when the evaluate itself errored or
        timed out -- callers treat that as "not ready yet" and retry.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=timeout))
            except Exception:
                return None
            if msg.get("id") == msg_id:
                return msg
        return None


def _profile_dir() -> Path:
    d = _config_dir() / "qoder_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_login_window(port: int = DEFAULT_PORT) -> None:
    """Open a visible Chrome on qoder.com for (re)login.

    Called from the settings dialog; the session cookie written by the login
    persists in the dedicated profile for headless fetches.

    The fetch path keeps a HEADLESS helper Chrome on the debug port; opening
    a tab there would be invisible, so close it first (Browser.close flushes
    cookies to disk) and launch a visible window in its place.
    """
    cdp = QoderCdp(port)
    exe = _find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge")
    if cdp.is_alive():
        close_browser(cdp)
    subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={_profile_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://qoder.com/account/usage",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if cdp.is_alive():
            return
        time.sleep(0.2)
    raise RuntimeError("登录窗口启动超时")


def close_browser(cdp: QoderCdp, settle: float = 0.8) -> bool:
    """Gracefully close the Chrome owning the debug port via CDP.

    Mirrors providers.volcengine_cdp.close_browser so the two providers have
    the same auto-close contract: Browser.close flushes the cookie profile
    to disk before the process dies (matters when a login just happened in
    a visible window), and we wait for the port to free up before the
    caller respawns on the same user-data-dir.

    Returns True if the Chrome is gone (and the caller can respawn). Callers
    that need the profile lock treat False as "retry later" instead of
    spawning a new Chrome that would forward to the zombie and time out.

    ``settle``: grace period AFTER the port dies but BEFORE the caller may
    respawn on the same profile. The port dying does not mean the process
    is gone -- it may still be releasing the user-data-dir singleton lock
    and flushing the last cookie writes. A Chrome spawned in that window
    forwards its command line to the dying process and exits, so its debug
    port never opens (the "spawn timeout" failure). Pass settle=0 only for
    teardown (app quit), never before a respawn.

    NOTE: can wait up to ~10s for the port to free up, so only call from
    a background thread -- never from the UI thread.
    """
    try:
        info = requests.get(f"{cdp.base}/json/version", timeout=5).json()
        ws_url = info.get("webSocketDebuggerUrl")
        if ws_url:
            from websockets.sync.client import connect

            with connect(ws_url, open_timeout=5, close_timeout=5) as ws:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
    except Exception:
        pass
    # Wait for the port to actually free up before relaunching. Short
    # liveness timeout -- a long one explodes this loop's wall-clock time
    # on machines where loopback connect() is slow to raise.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not cdp.is_alive(timeout=0.5):
            time.sleep(settle)
            return True
        time.sleep(0.2)
    return False


@register_provider
class QoderProvider(ProviderBase):
    type_name = "qoder"

    def fetch(self) -> ProviderSnapshot:
        port = int(self.credentials.get("cdp_port", DEFAULT_PORT))
        cdp = QoderCdp(port)
        # If a Chrome is already alive on this port it's the visible login
        # window (open_login_window killed the headless one before launching
        # visible). Close it before spawning headless so the user isn't staring
        # at a Chrome window we just stole focus onto, and so eval_fetch's
        # navigation doesn't yank focus to whatever tab is foreground.
        # Mirrors VolcengineProvider._fetch_locked's LOGIN_PORT pattern.
        if cdp.is_alive(timeout=0.5):
            close_browser(cdp)
        cdp.spawn(headless=True)
        results = cdp.eval_fetch()
        if not results:
            raise RuntimeError("Qoder 返回为空")

        for res in results:
            if res.get("status") == 401:
                raise RuntimeError("Qoder 登录已过期,请在设置中重新登录")

        v2 = results[0].get("body") or {}
        v1 = (results[1].get("body") or {}) if len(results) > 1 else {}
        plan = (v2.get("plan_quota") or {}).get("quota_summary") or {}
        shared = (v1.get("shared_quota") or {}).get("quota_summary") or {}
        reset_ms = v2.get("nextResetAt")
        reset_s = int(reset_ms / 1000) if isinstance(reset_ms, (int, float)) else None

        levels: list[QuotaLevel] = []
        # Org-shared pool first: it is the actively-consumed quota once the
        # personal plan runs dry (ball defaults to levels[0]).
        # Levels render as percentages so the ball keeps its 80%/95% warning
        # colors; we compute percent from used/limit ourselves (server's
        # usage_percentage is rounded to whole numbers, so without this the
        # 6000-credit pool only updates once every ~60 credits consumed).
        # The exact credit numbers go to extra_lines.
        def pct(used: int | None, limit: int | None) -> float:
            if not used or not limit:
                return 0.0
            return round(used / limit * 100, 2)

        if shared.get("limit_value"):
            levels.append(
                QuotaLevel(
                    level="addon",
                    label="组织池",
                    used_percent=pct(shared.get("used_value"),
                                       shared.get("limit_value")),
                    reset_timestamp=reset_s,
                )
            )
        if plan.get("limit_value"):
            levels.append(
                QuotaLevel(
                    level="plan",
                    label="套餐",
                    used_percent=pct(plan.get("used_value"),
                                       plan.get("limit_value")),
                    reset_timestamp=reset_s,
                )
            )
        if not levels:
            raise RuntimeError("Qoder 未返回配额数据")

        extra: list[str] = []
        if plan.get("limit_value"):
            extra.append(
                f"套餐 {plan['used_value']:,} / {plan['limit_value']:,}"
                f" · 剩 {plan.get('remaining_value', 0):,}"
            )
        if shared.get("limit_value"):
            extra.append(
                f"组织池 {shared['used_value']:,} / {shared['limit_value']:,}"
                f" · 剩 {shared.get('remaining_value', 0):,}"
            )
        pool = v1.get("organization_pool")
        if isinstance(pool, dict) and pool.get("limit_value"):
            extra.append(
                f"组织总池 {pool['used_value']:,} / {pool['limit_value']:,}"
            )

        return ProviderSnapshot(
            provider_id=self.provider_id,
            ok=True,
            error="",
            levels=levels,
            extra_lines=extra,
        )
