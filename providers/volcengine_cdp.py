"""Volcano Engine Ark coding-plan provider via dedicated Chrome + CDP.

Mirrors the Qoder pattern: a dedicated Chrome instance with --remote-debugging-port,
running the API request via ``Runtime.evaluate`` so the browser handles all the
headers (x-csrf-token / x-web-id / Cookie) automatically. We never need to
intercept the request -- the page's own fetch interceptor adds the right
headers, and the session cookie rides along because we're in the page origin.

Approach:
- spawn ``chrome.exe --remote-debugging-port=<port> --user-data-dir=<profile>``
  (the profile dir MUST be a dedicated one next to config.json)
- talk raw CDP over websocket (only ``Runtime.evaluate`` is needed)
- first login (and re-login every ~48 h when the session expires) happens in a
  visible window opened by ``open_login_window()``; the cookie persists in the
  dedicated profile, so subsequent polls can run headless against the same
  profile.

Login detection: ``verify_session`` runs the real quota-API fetch inside an
existing console.volcengine.com tab and only trusts a 200 response carrying a
Result payload. Cookie PRESENCE must never be used as the signal -- tracking
cookies (csrfToken, __tea_*, ...) linger in the profile for a long time after
the session expires, which is exactly how an earlier version reported
"登录成功" on a dead session.

Logging: ``stderr`` (always) + ``volc_cdp_debug.log`` (when
``VOLC_CDP_DEBUG=1``). Every CDP round-trip and decision is logged so
real-Chrome issues can be diagnosed from the log without re-running.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

CODING_PLAN_URL = (
    "https://console.volcengine.com/ark/region:cn-beijing/subscription/coding-plan"
)
API_URL = (
    "https://console.volcengine.com/api/top/ark/cn-beijing/"
    "2024-01-01/GetCodingPlanUsage"
)
# Two ports on purpose. The visible login window (open_login_window) lives
# on LOGIN_PORT; the headless polling Chrome (spawned on every fetch()) lives
# on DEFAULT_PORT. Both share the same user-data-dir so cookies set in the
# visible window are visible to the headless one. Keeping them on separate
# ports is the only way to guarantee `cdp.spawn(headless=True)` actually
# starts a headless instance -- if they shared a port, the visible window
# would squat it and `is_alive()` would return True, so spawn() would be a
# no-op and eval_fetch()'s /json/new would create new tabs on the user's
# visible window, stealing focus every poll cycle.
DEFAULT_PORT = 9334
LOGIN_PORT = 9335


class LoginWindowOpen(RuntimeError):
    """Raised by fetch() while the visible login window is open and the user
    has not logged in yet. The caller should skip this provider for the
    cycle (keep the previous snapshot) instead of showing an error."""


class ProfileBusy(RuntimeError):
    """Raised by profile_op when another thread is already starting /
    stopping a Chrome on the shared profile dir. Callers should treat it
    as "try again later", never as a hard error on the ball."""


# Chrome's process singleton: only ONE Chrome at a time may run on the
# shared user-data-dir. fetch() (background thread), open_login_window()
# (UI thread, button click) and the settings-dialog probe can all try to
# spawn/close Chrome concurrently -- without serialization, two overlapping
# spawn sequences race for the singleton and whichever Chrome starts
# second just forwards its command line to the first and exits, so its
# debug port never opens (15 s spawn timeout, red ball / login window
# that flashes and dies). Every start/stop sequence on this profile must
# hold this lock.
_PROFILE_LOCK = threading.Lock()


@contextmanager
def profile_op(timeout: float = 0.0):
    """Serialize Chrome start/stop sequences on the shared profile dir.

    ``timeout=0`` (default) never blocks: contention raises ProfileBusy
    immediately, which fetch() maps to a skipped cycle. Callers that act
    on a user click (open_login_window) may pass a small timeout so a
    brief in-flight fetch doesn't fail the click outright.
    """
    if not _PROFILE_LOCK.acquire(timeout=timeout):
        raise ProfileBusy("浏览器配置正被其他操作使用，请稍后重试")
    try:
        yield
    finally:
        _PROFILE_LOCK.release()


def session_is_valid(res) -> bool:
    """True iff a verify_session / eval_fetch result dict PROVES a logged-in
    session: HTTP 200 and a Result payload. Everything else -- 401 NotLogin,
    a -1 fetch error, a non-dict body, or None -- is not a session."""
    if not isinstance(res, dict) or res.get("status") != 200:
        return False
    body = res.get("body")
    return isinstance(body, dict) and bool(body.get("Result"))

# ---------------------------------------------------------------------------
# Logging -- always to stderr AND to a rotating volc_cdp_debug.log in CWD.
# The file log is ALWAYS on (no env var): CDP issues only reproduce in real
# usage, and a log that needs MONITOR_DEBUG=1 set beforehand never captures
# the failure. 500 KB x 2 backups keeps disk usage trivial.
# ---------------------------------------------------------------------------
_log = logging.getLogger("volcengine_cdp")
if not _log.handlers:
    _log.setLevel(logging.DEBUG)
    _stderr = logging.StreamHandler(sys.stderr)
    _stderr.setFormatter(
        logging.Formatter("[volc] %(asctime)s.%(msecs)03d "
                          "%(levelname)-5s %(message)s",
                          datefmt="%H:%M:%S")
    )
    _log.addHandler(_stderr)
    try:
        from logging.handlers import RotatingFileHandler

        _file = RotatingFileHandler(
            "volc_cdp_debug.log", maxBytes=500_000, backupCount=2,
            encoding="utf-8",
        )
        _file.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
                datefmt="%H:%M:%S")
        )
        _log.addHandler(_file)
    except OSError:
        pass
# Prevent double-logging if root logger is configured.
_log.propagate = False

# JS executed via CDP: wait until the tab has landed on console.volcengine.com
# AND finished loading, then POST to the quota API from the page origin
# (cookies + headers handled by the page's interceptor), and return the
# response.
_EVAL_FETCH = """
(async () => {
  const url = "__API_URL__";
  const t0 = Date.now();
  // Both conditions matter. Landing on the console origin gates the fetch
  // on the navigation committing; readyState "complete" gates it on the
  // page having loaded. At commit time (readyState "interactive") the
  // server has not activated the csrf token for this page load yet -- a
  // fetch fired there returns 200 + InvalidCSRFToken with the very same
  // cookie that succeeds a few hundred ms later (verified against the
  // live API). This race was 100% reproducible on a freshly spawned
  // Chrome, so waiting for "complete" is not optional.
  while ((document.readyState !== "complete" ||
          !location.href.startsWith("https://console.volcengine.com")) &&
         Date.now() - t0 < 15000) {
    await new Promise(r => setTimeout(r, 200));
  }
  if (!location.href.startsWith("https://console.volcengine.com")) {
    return [{status: -1, body: {error: "navigation timeout: " + location.href}}];
  }
  // The console API requires the x-csrf-token header; the page's axios layer
  // normally injects it (from the csrfToken cookie), but a raw fetch() does
  // not go through axios -- without this the API answers
  // "无效的csrf token" even with a perfectly valid session.
  const csrf = () =>
    (document.cookie.match(/(?:^|;\\s*)csrfToken=([^;]+)/) || [])[1] || "";
  const doFetch = async () => {
    const r = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-csrf-token": csrf()
      },
      body: "{}"
    });
    let body;
    try { body = await r.json(); } catch (e) { body = {error: "non-json response"}; }
    return {status: r.status, body: body};
  };
  try {
    let out = await doFetch();
    // Belt and braces for slow-settling pages: if the first fetch still
    // raced the server-side session setup, wait briefly and retry once.
    const errCode =
      ((((out.body || {}).ResponseMetadata || {}).Error || {}).Code) || "";
    if (String(errCode).toLowerCase().includes("csrf")) {
      await new Promise(r => setTimeout(r, 800));
      out = await doFetch();
    }
    return [out];
  } catch (e) {
    return [{status: -1, body: {error: String(e)}}];
  }
})()
""".replace("__API_URL__", API_URL)

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _profile_dir() -> Path:
    """Chrome user-data-dir lives next to config so the profile survives
    across runs and the encrypted cookies stay tied to this install."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "volcengine_profile"
    return Path(__file__).resolve().parent.parent / "volcengine_profile"


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


class VolcengineCdp:
    """Minimal CDP client: one page target + Runtime.evaluate + getCookies."""

    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"

    def is_alive(self, timeout: float = 2) -> bool:
        """Whether a Chrome answers on this debug port.

        ``timeout`` matters more than it looks: on machines where loopback
        traffic is intercepted by security software, a REFUSED connection
        raises after ~2 s, not instantly -- polling loops that called this
        with a big timeout turned a nominal "wait 10 s" into 100+ s."""
        try:
            r = requests.get(f"{self.base}/json/version", timeout=timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def verify_session(self, allow_navigate: bool = False) -> dict | None:
        """REAL login validity check: run the quota API fetch inside an
        existing console.volcengine.com tab on this port.

        Cookie PRESENCE is not enough: expired cookies linger in the shared
        profile, which made the old probe report "登录成功" the moment the
        login window opened -- the window then got auto-closed before the
        user could actually re-login, and every fetch failed with
        "无效的csrf token". Only a 200 with quota data proves the session.

        Non-intrusive by design: no new tab, no navigation -- we evaluate
        in whatever console tab already exists, so a visible login window
        never loses focus. Pass allow_navigate=True ONLY for the headless
        polling Chrome (navigation is invisible there): a freshly spawned
        instance has nothing but about:blank, so without navigation the
        check would always fail even with a perfectly valid session.

        Returns the fetch result dict ({status, body}), or None when there
        is no usable console tab / CDP failed.
        """
        try:
            tabs = requests.get(f"{self.base}/json/list", timeout=3).json()
        except requests.RequestException as e:
            _log.warning(f"verify_session: /json/list failed: {e}")
            return None
        target = next(
            (t for t in tabs
             if t.get("type") == "page"
             and str(t.get("url", "")).startswith("https://console.volcengine.com")),
            None,
        )
        if not target:
            if not allow_navigate:
                _log.info("verify_session: no console.volcengine.com tab open "
                          f"(tabs: {[t.get('url', '?')[:60] for t in tabs[:3]]})")
                return None
            # Headless instance: open a tab, navigate, evaluate, close it.
            _log.info("verify_session: no console tab; falling back to eval_fetch")
            try:
                results = self.eval_fetch()
                return results[0] if results else None
            except Exception as e:
                _log.warning(f"verify_session: eval_fetch fallback failed: {e}")
                return None
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        try:
            from websockets.sync.client import connect
        except ImportError:
            _log.error("websockets module not installed")
            return None
        try:
            with connect(ws_url, open_timeout=5, close_timeout=3) as ws:
                self._send(
                    ws, 1, "Runtime.evaluate",
                    expression=_EVAL_FETCH, awaitPromise=True,
                    returnByValue=True,
                )
                deadline = time.time() + 30
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=30))
                    if msg.get("id") == 1:
                        result = msg.get("result", {}).get("result", {})
                        value = result.get("value") if result.get("type") == "object" else None
                        if isinstance(value, list) and value:
                            out = value[0]
                            _log.info(f"verify_session: status={out.get('status')}")
                            return out
                        _log.warning(f"verify_session: malformed eval reply: {msg.get('result') or msg.get('error')}")
                        return None
        except Exception as e:
            _log.warning(f"verify_session: WS error: {e}")
        return None

    def spawn(self, headless: bool = True) -> None:
        """Start a dedicated Chrome with the debug port. No-op if alive."""
        if self.is_alive():
            _log.debug(f"Chrome already alive on port {self.port}")
            return
        exe = _find_browser()
        if not exe:
            raise RuntimeError("未找到 Chrome/Edge，请安装 Chrome 后重试")
        _profile_dir().mkdir(parents=True, exist_ok=True)
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
        _log.info(f"spawning Chrome (headless={headless}) on port {self.port}")
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if headless else 0,
        )
        # Deadline-based with a SHORT liveness timeout: a refused loopback
        # connection can take seconds to raise on some machines (see
        # is_alive), which once turned this nominal ~10 s loop into 110 s.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.is_alive(timeout=0.5):
                _log.info(f"Chrome up on port {self.port}")
                return
            time.sleep(0.2)
        raise RuntimeError("Chrome 调试端口启动超时")

    def eval_fetch(self) -> list[dict]:
        """Open a tab on the Coding Plan page, evaluate the fetch, return results.

        The tab is created on about:blank and navigated explicitly; we then
        poll ``location.href`` until the console.volcengine.com execution
        context is live. Evaluating during navigation fails with "execution
        context destroyed" -- running the fetch promise blindly right after
        /json/new races the navigation and loses.
        """
        # Newer Chrome requires PUT for /json/new.
        try:
            r = requests.put(f"{self.base}/json/new?about:blank", timeout=10)
            if r.status_code != 200:
                r = requests.get(f"{self.base}/json/new?about:blank", timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            _log.error(f"/json/new failed: {e}")
            raise
        target = r.json()
        ws_url = target["webSocketDebuggerUrl"]
        target_id = target["id"]
        _log.info(f"eval_fetch: opened tab {target_id}, navigating to coding-plan")
        try:
            from websockets.sync.client import connect
        except ImportError as e:
            raise RuntimeError("缺少 websockets 依赖 (pip install websockets)") from e

        try:
            with connect(ws_url, open_timeout=10, close_timeout=5) as ws:
                self._send(ws, 1, "Runtime.enable")
                self._send(ws, 2, "Page.enable")
                self._send(ws, 3, "Page.navigate", url=CODING_PLAN_URL)
                msg_id = 4
                deadline = time.time() + 30
                ready = False
                polls = 0
                last_href = ""
                while time.time() < deadline and not ready:
                    self._send(
                        ws, msg_id, "Runtime.evaluate",
                        expression="location.href", returnByValue=True,
                    )
                    reply = self._recv_for(ws, msg_id)
                    # CDP evaluate replies nest: {result: {result: RemoteObject}}.
                    res = ((reply or {}).get("result") or {}).get("result", {})
                    if (res.get("type") == "string"
                            and res.get("value", "").startswith(
                                "https://console.volcengine.com")):
                        ready = True
                        _log.info(f"page ready after {polls} poll(s)")
                    else:
                        href = res.get("value", "?") if res else "?"
                        if href != last_href:
                            _log.debug(f"page href={href!r} (poll {polls})")
                            last_href = href
                        time.sleep(0.3)
                        msg_id += 1
                        polls += 1
                if not ready:
                    _log.error(f"page never reached console.volcengine.com "
                               f"after {polls} polls; last href={last_href!r}")
                    raise RuntimeError("console.volcengine.com 页面加载超时")
                msg_id += 1
                self._send(
                    ws, msg_id, "Runtime.evaluate",
                    expression=_EVAL_FETCH,
                    awaitPromise=True,
                    returnByValue=True,
                )
                _log.info(f"eval_fetch: page-side fetch started")
                # Skip replies until our evaluate id comes back.
                deadline = time.time() + 40
                while time.time() < deadline:
                    msg = json.loads(ws.recv(timeout=40))
                    if msg.get("id") == msg_id:
                        result = msg.get("result", {}).get("result", {})
                        if result.get("type") == "object" and "value" in result:
                            value = result["value"]
                            if value:
                                first = value[0] if isinstance(value, list) else value
                                status = first.get("status") if isinstance(first, dict) else "?"
                                _log.info(f"eval_fetch done: status={status}")
                            else:
                                _log.warning("eval_fetch returned empty value")
                            return value
                        _log.error(f"CDP eval returned malformed: "
                                   f"{msg.get('result') or msg.get('error')}")
                        raise RuntimeError(
                            f"CDP 返回异常: {msg.get('result') or msg.get('error')}"
                        )
        finally:
            try:
                requests.get(f"{self.base}/json/close/{target_id}", timeout=5)
            except requests.RequestException:
                pass
        _log.error("eval_fetch timed out waiting for CDP reply")
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


def open_login_window(port: int = LOGIN_PORT) -> None:
    """Open a visible Chrome on console.volcengine.com for (re)login.

    Uses LOGIN_PORT (separate from DEFAULT_PORT) so the headless polling
    Chrome (managed by VolcengineCdp on DEFAULT_PORT) can never accidentally
    serve eval_fetch() against this window -- otherwise every 10-second poll
    would create a new tab on the user's visible browser and steal focus.

    The session cookie written by the login persists in the dedicated
    profile (shared with the headless Chrome) for fetches.

    CRITICAL: the profile dir is shared with the headless polling Chrome on
    DEFAULT_PORT. Chrome's process singleton means a second Chrome started
    with the same user-data-dir just forwards its command line to the
    running instance and exits -- the visible window would never appear and
    the debug port would never open (this is exactly the "browser flashes
    and closes" failure). So the headless instance MUST be closed first.
    Browser.close flushes cookies to disk before exiting, so nothing is
    lost by closing it.
    """
    cdp = VolcengineCdp(port)
    exe = _find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge")
    # Hold the profile lock across close+spawn: a concurrent fetch() spawning
    # headless Chrome in the same instant would race the login window for
    # the singleton lock and one of the two would die. Brief blocking is
    # fine here (this runs on a user click; a fetch cycle takes a few s).
    with profile_op(timeout=5.0):
        if port != DEFAULT_PORT:
            headless = VolcengineCdp(DEFAULT_PORT)
            if headless.is_alive(timeout=0.5):
                _log.info("closing headless polling Chrome to free the profile lock")
                if not close_browser(headless):
                    raise RuntimeError(
                        "无法释放 Chrome 配置目录（后台轮询实例未退出），请稍后重试"
                    )
        if cdp.is_alive(timeout=0.5):
            _log.info(f"closing existing Chrome on port {port} before relaunch")
            if not close_browser(cdp):
                raise RuntimeError(f"端口 {port} 上的浏览器未能关闭，请稍后重试")
        _profile_dir().mkdir(parents=True, exist_ok=True)
        _log.info(f"opening login window on port {port} -> {CODING_PLAN_URL}")
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={_profile_dir()}",
                "--no-first-run",
                "--no-default-browser-check",
                CODING_PLAN_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cdp.is_alive(timeout=0.5):
                _log.info(f"login window up on port {port}")
                return
            time.sleep(0.2)
    raise RuntimeError("登录窗口启动超时")


def close_browser(cdp, settle: float = 0.8) -> bool:
    """Gracefully close the Chrome owning the debug port via CDP.

    Browser.close makes Chrome flush cookies to disk before exiting, which
    matters when a login just happened in a visible window.

    Returns True when the port is free afterwards. Callers that need the
    profile lock (open_login_window / fetch) treat False as "retry later"
    instead of spawning a new Chrome that would forward to the zombie and
    time out.

    ``settle``: grace period AFTER the port goes down but BEFORE the caller
    may respawn on the same profile. The port dying does not mean the
    process is gone -- it may still be releasing the user-data-dir
    singleton lock and flushing the last cookie writes. A Chrome spawned
    in that window forwards its command line to the dying process and
    exits, so its debug port never opens (the "spawn timeout" failure).
    Pass settle=0 only for teardown (app quit), never before a respawn.

    NOTE: can wait up to ~10s for the port to free up, so only call from a
    background thread -- never from the UI thread.
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
    # liveness timeout -- see is_alive for why a long one explodes this
    # loop's wall-clock time.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not cdp.is_alive(timeout=0.5):
            time.sleep(settle)
            return True
        time.sleep(0.2)
    _log.warning(f"port {cdp.port} still occupied after Browser.close")
    return False