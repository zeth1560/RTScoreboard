"""Restart OBS Studio (Windows): kill hung instance, relaunch, optionally start replay buffer via WebSocket."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from scoreboard.config.settings import Settings

_LOG = logging.getLogger(__name__)

_DEFAULT_OBS_CANDIDATES = (
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files\obs-studio\bin\32bit\obs32.exe",
)


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


def _launch_obs(exe: str) -> None:
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe],
            cwd=str(Path(exe).parent),
            close_fds=True,
            creationflags=creationflags,
        )
    else:
        subprocess.Popen(
            [exe],
            cwd=str(Path(exe).parent),
            close_fds=True,
            start_new_session=True,
        )


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

    timeout = max(settings.obs_websocket_timeout_sec, 2.0)
    try:
        with obs.ReqClient(
            host=settings.obs_websocket_host,
            port=settings.obs_websocket_port,
            password=settings.obs_websocket_password or "",
            timeout=timeout,
        ) as client:
            client.start_replay_buffer()
    except (OBSSDKTimeoutError, OBSSDKError, OSError) as e:
        _LOG.warning("OBS restart: could not start replay buffer via WebSocket: %s", e)
        return False
    _LOG.info("OBS replay buffer start requested via WebSocket")
    return True


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
    _taskkill_obs_processes()
    time.sleep(0.5)

    try:
        _launch_obs(exe)
    except OSError as e:
        _LOG.exception("Failed to launch OBS")
        return (False, f"Failed to launch OBS: {e}")

    if settings.obs_restart_start_replay_buffer:
        delay = settings.obs_restart_post_launch_delay_ms / 1000.0
        _LOG.info(
            "OBS relaunched; waiting %.1fs before starting replay buffer",
            delay,
        )
        time.sleep(delay)
        if try_start_replay_buffer(settings):
            return (True, "OBS restarted; replay buffer start sent")
        return (True, "OBS restarted; replay buffer could not be started (see logs)")

    return (True, "OBS restarted")
