"""Restart OBS Studio (Windows): kill hung instance, relaunch, optionally start replay buffer via WebSocket."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
import ctypes
from pathlib import Path

from scoreboard.config.settings import Settings

_LOG = logging.getLogger(__name__)

_DEFAULT_OBS_CANDIDATES = (
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files\obs-studio\bin\32bit\obs32.exe",
)


def _restart_ws_hosts(settings: Settings) -> list[str]:
    preferred = (settings.obs_websocket_host or "").strip() or "localhost"
    ordered = [preferred, "127.0.0.1", "localhost"]
    out: list[str] = []
    seen: set[str] = set()
    for h in ordered:
        k = h.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def _status_active(obj: object) -> bool:
    v = getattr(obj, "output_active", None)
    if v is None:
        v = getattr(obj, "outputActive", False)
    return bool(v)


def resolve_obs_executable(settings: Settings) -> str | None:
    raw = (settings.obs_executable or "").strip()
    if raw:
        p = Path(raw)
        return str(p) if p.is_file() else None
    for candidate in _DEFAULT_OBS_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _taskkill_obs_processes() -> None:
    if os.name != "nt":
        return
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for im in ("obs64.exe", "obs32.exe", "obs.exe"):
        r = subprocess.run(
            ["taskkill", "/F", "/IM", im, "/T"],
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=creationflags,
        )
        if r.returncode == 0:
            _LOG.info("taskkill stopped %s", im)
        else:
            out = (r.stdout or "") + (r.stderr or "")
            if "not found" in out.lower() or "not running" in out.lower():
                _LOG.debug("taskkill %s: %s", im, out.strip() or r.returncode)
            else:
                _LOG.debug(
                    "taskkill %s exit=%s: %s",
                    im,
                    r.returncode,
                    out.strip() or "(no output)",
                )


def _obs_pids() -> set[int]:
    if os.name != "nt":
        return set()
    pids: set[int] = set()
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _enum(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _pid_image_name(int(pid.value))
        if image in ("obs64.exe", "obs32.exe", "obs.exe"):
            pids.add(int(pid.value))
        return True

    user32.EnumWindows(EnumWindowsProc(_enum), 0)
    return pids


def _try_graceful_close_obs(timeout_sec: float = 10.0) -> bool:
    """Try WM_CLOSE first so OBS exits cleanly (prevents recovery/safe-mode prompt)."""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    before = _obs_pids()
    if not before:
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _enum(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in before:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(EnumWindowsProc(_enum), 0)
    deadline = time.monotonic() + max(2.0, timeout_sec)
    while time.monotonic() < deadline:
        _auto_confirm_obs_dialogs(before)
        remaining = _obs_pids()
        if not remaining.intersection(before):
            return True
        time.sleep(0.5)
    return False


def _auto_confirm_obs_dialogs(obs_pids: set[int]) -> None:
    """Auto-click confirmation buttons on OBS-owned dialogs during shutdown."""
    if os.name != "nt" or not obs_pids:
        return
    user32 = ctypes.windll.user32
    BM_CLICK = 0x00F5
    target_labels = {"ok", "yes", "launch normally"}

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _window_text(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return (buf.value or "").strip()

    def _class_name(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return (buf.value or "").strip()

    def _enum_windows(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in obs_pids:
            return True
        # OBS dialogs on Windows are typically standard dialog class.
        if _class_name(hwnd) != "#32770":
            return True

        clicked = {"ok": False}

        def _enum_child(ch, _lp):
            if _class_name(ch) != "Button":
                return True
            text = _window_text(ch).lower()
            if text in target_labels:
                user32.PostMessageW(ch, BM_CLICK, 0, 0)
                clicked["ok"] = True
                return False
            return True

        user32.EnumChildWindows(hwnd, EnumChildProc(_enum_child), 0)
        if clicked["ok"]:
            _LOG.info("OBS restart: auto-confirmed OBS dialog: %r", _window_text(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(_enum_windows), 0)


def _auto_select_obs_startup_normal_mode() -> bool:
    """
    During OBS launch, auto-select normal startup on recovery/safe-mode prompts.
    Returns True if any dialog button was clicked.
    """
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    BM_CLICK = 0x00F5
    clicked_any = {"ok": False}
    # Include common wording variants.
    target_labels = {
        "launch normally",
        "normal mode",
        "run in normal mode",
        "ok",
        "yes",
    }

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _window_text(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return (buf.value or "").strip()

    def _class_name(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buf, 128)
        return (buf.value or "").strip()

    def _window_belongs_to_obs(hwnd: int) -> bool:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _pid_image_name(int(pid.value))
        return image in ("obs64.exe", "obs32.exe", "obs.exe")

    def _enum_windows(hwnd, _lparam):
        if _class_name(hwnd) != "#32770":
            return True
        title = _window_text(hwnd).lower()
        if "obs" not in title and not _window_belongs_to_obs(hwnd):
            return True

        clicked = {"ok": False}

        def _enum_child(ch, _lp):
            if _class_name(ch) != "Button":
                return True
            text = _window_text(ch).strip().lower()
            if text in target_labels or "normal mode" in text:
                user32.PostMessageW(ch, BM_CLICK, 0, 0)
                clicked["ok"] = True
                return False
            return True

        user32.EnumChildWindows(hwnd, EnumChildProc(_enum_child), 0)
        if clicked["ok"]:
            clicked_any["ok"] = True
            _LOG.info("OBS restart: auto-selected normal startup dialog option: %r", title)
        return True

    user32.EnumWindows(EnumWindowsProc(_enum_windows), 0)
    return clicked_any["ok"]


def _try_stop_outputs_before_close(settings: Settings) -> bool:
    """
    Best-effort: stop OBS outputs first so WM_CLOSE won't trigger confirmation dialog.
    Returns True when a websocket connection succeeded (even if nothing was active).
    """
    try:
        import obsws_python as obs
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError
    except ImportError:
        return False

    timeout = max(settings.obs_websocket_timeout_sec, 3.0)
    for host in _restart_ws_hosts(settings):
        try:
            with obs.ReqClient(
                host=host,
                port=settings.obs_websocket_port,
                password=settings.obs_websocket_password or "",
                timeout=timeout,
            ) as client:
                try:
                    if _status_active(client.get_stream_status()):
                        client.stop_stream()
                        _LOG.info("OBS restart: stopped active stream")
                except Exception:
                    _LOG.debug("OBS restart: stream stop skipped", exc_info=True)
                try:
                    if _status_active(client.get_record_status()):
                        client.stop_record()
                        _LOG.info("OBS restart: stopped active recording")
                except Exception:
                    _LOG.debug("OBS restart: record stop skipped", exc_info=True)
                try:
                    if _status_active(client.get_replay_buffer_status()):
                        client.stop_replay_buffer()
                        _LOG.info("OBS restart: stopped active replay buffer")
                except Exception:
                    _LOG.debug("OBS restart: replay buffer stop skipped", exc_info=True)
                try:
                    if _status_active(client.get_virtual_cam_status()):
                        client.stop_virtual_cam()
                        _LOG.info("OBS restart: stopped active virtual camera")
                except Exception:
                    _LOG.debug("OBS restart: virtual cam stop skipped", exc_info=True)
                return True
        except (OBSSDKTimeoutError, OBSSDKError, OSError):
            continue
    return False


def _parse_launch_args(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    try:
        return shlex.split(s, posix=False)
    except ValueError:
        _LOG.warning("OBS_RESTART_LAUNCH_ARGS parse failed; using raw string as single arg")
        return [s]


def _launch_obs(exe: str, launch_args: list[str]) -> int:
    cmd = [exe, *launch_args]
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(exe).parent),
            close_fds=True,
            creationflags=creationflags,
        )
        return int(proc.pid)
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(exe).parent),
            close_fds=True,
            start_new_session=True,
        )
        return int(proc.pid)


def _pid_image_name(pid: int) -> str:
    if os.name != "nt":
        return ""
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(h)


def _try_restore_any_obs_window_normal() -> bool:
    """Best-effort: restore any OBS window(s) to normal mode."""
    if os.name != "nt":
        return False

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOW = 5
    found = {"count": 0}

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _enum(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _pid_image_name(int(pid.value))
        if image not in ("obs64.exe", "obs32.exe", "obs.exe"):
            return True
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        found["count"] += 1
        return True

    user32.EnumWindows(EnumWindowsProc(_enum), 0)
    return found["count"] > 0


def _wait_for_obs_window_and_restore(timeout_sec: float = 30.0) -> bool:
    if os.name != "nt":
        return False
    deadline = time.monotonic() + max(2.0, timeout_sec)
    while time.monotonic() < deadline:
        _auto_select_obs_startup_normal_mode()
        if _try_restore_any_obs_window_normal():
            return True
        time.sleep(0.5)
    return False


def try_start_replay_buffer(settings: Settings) -> bool:
    """Connect to OBS WebSocket and start the replay buffer. Best-effort."""
    try:
        import obsws_python as obs
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError
    except ImportError:
        _LOG.warning(
            "OBS restart: replay buffer not started (obsws-python not installed)",
        )
        return False

    timeout = max(settings.obs_websocket_timeout_sec, 5.0)
    # obsws_python logs verbose tracebacks on timeouts; keep logs readable.
    logging.getLogger("obsws_python.baseclient").setLevel(logging.CRITICAL)
    last_error: Exception | None = None
    for host in _restart_ws_hosts(settings):
        try:
            with obs.ReqClient(
                host=host,
                port=settings.obs_websocket_port,
                password=settings.obs_websocket_password or "",
                timeout=timeout,
            ) as client:
                # If already active, treat that as success.
                try:
                    status = client.get_replay_buffer_status()
                    active = getattr(status, "output_active", None)
                    if active is None:
                        active = getattr(status, "outputActive", False)
                    if active:
                        _LOG.info("OBS replay buffer already active (host=%s)", host)
                        return True
                except Exception:
                    _LOG.debug("OBS replay buffer status pre-check failed", exc_info=True)
                client.start_replay_buffer()
                try:
                    status_after = client.get_replay_buffer_status()
                    active_after = getattr(status_after, "output_active", None)
                    if active_after is None:
                        active_after = getattr(status_after, "outputActive", False)
                    if active_after:
                        _LOG.info("OBS replay buffer confirmed active (host=%s)", host)
                        return True
                except Exception:
                    _LOG.debug("OBS replay buffer status post-check failed", exc_info=True)
                _LOG.info("OBS replay buffer start requested via WebSocket (host=%s)", host)
                return True
        except (OBSSDKTimeoutError, OBSSDKError, OSError) as e:
            last_error = e
            _LOG.warning(
                "OBS restart: replay buffer WebSocket failed host=%s port=%s timeout=%.1fs: %s",
                host,
                settings.obs_websocket_port,
                timeout,
                e,
            )
            continue
    if last_error is not None:
        _LOG.warning("OBS restart: could not start replay buffer via WebSocket after host fallbacks")
        return False
    _LOG.info("OBS replay buffer start requested via WebSocket")
    return True


def _start_replay_buffer_with_retries(settings: Settings, total_wait_sec: float) -> bool:
    deadline = time.monotonic() + max(3.0, total_wait_sec)
    while time.monotonic() < deadline:
        if try_start_replay_buffer(settings):
            return True
        time.sleep(1.0)
    return False


def restart_obs_pipeline(settings: Settings) -> tuple[bool, str]:
    """
    Kill OBS, relaunch the executable (normal GUI), optionally enable replay buffer after a delay.

    Intended to run on a background thread.
    """
    if os.name != "nt":
        return (False, "OBS auto-restart is only supported on Windows")

    exe = resolve_obs_executable(settings)
    if not exe:
        return (
            False,
            "OBS executable not found — set OBS_EXECUTABLE or install OBS Studio",
        )

    _LOG.info("OBS restart pipeline: stopping existing OBS processes")
    ws_stopped = _try_stop_outputs_before_close(settings)
    if ws_stopped:
        # Give OBS a moment to settle output state before closing.
        time.sleep(0.8)
    closed_cleanly = _try_graceful_close_obs(timeout_sec=12.0)
    if not closed_cleanly:
        _LOG.warning("OBS graceful close timed out; forcing taskkill fallback")
        _taskkill_obs_processes()
    time.sleep(0.5)
    launch_args = _parse_launch_args(settings.obs_restart_launch_args)
    _LOG.info("OBS restart launch args: %s", launch_args)

    try:
        _launch_obs(exe, launch_args)
    except OSError as e:
        _LOG.exception("Failed to launch OBS")
        return (False, f"Failed to launch OBS: {e}")

    restored = _wait_for_obs_window_and_restore(timeout_sec=30.0)
    if restored:
        _LOG.info("OBS window restored to normal mode")
    else:
        _LOG.warning("Could not confirm OBS window restore to normal mode")

    if settings.obs_restart_start_replay_buffer:
        delay = settings.obs_restart_post_launch_delay_ms / 1000.0
        _LOG.info(
            "OBS relaunched; waiting %.1fs before starting replay buffer",
            delay,
        )
        time.sleep(delay)
        if _start_replay_buffer_with_retries(settings, total_wait_sec=60.0):
            return (True, "OBS restarted; replay buffer start sent")
        return (True, "OBS restarted; replay buffer could not be started (see logs)")

    return (True, "OBS restarted")
