"""Spawn ReplayTrove launcher ``restart_obs.ps1`` (same OBS flags as launcher_ui)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from scoreboard.config.settings import Settings

_LOG = logging.getLogger(__name__)

_POWERSHELL = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe",
)


def request_launcher_obs_restart(settings: Settings, reason: str) -> None:
    """
    Non-blocking: start PowerShell to run the launcher script (stop obs64, sentinel, start OBS).

    Only when ``replay_launcher_restart_obs_on_unavailable`` is true and ``os.name == 'nt'``.
    """
    if os.name != "nt":
        _LOG.debug("Launcher OBS restart skipped (not Windows) reason=%r", reason)
        return
    if not settings.replay_launcher_restart_obs_on_unavailable:
        return
    script = (settings.replay_launcher_restart_obs_script or "").strip()
    if not script:
        return
    ps_path = Path(script)
    if not ps_path.is_file():
        _LOG.warning(
            "Launcher OBS restart skipped (script missing): %s reason=%r",
            script,
            reason,
        )
        return
    if not os.path.isfile(_POWERSHELL):
        _LOG.warning("Launcher OBS restart skipped (powershell.exe missing)")
        return

    resolved = str(ps_path.resolve())
    cmd = [
        _POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        resolved,
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ps_path.parent),
            creationflags=creationflags,
        )
        _LOG.info(
            "Spawned launcher OBS restart script=%s reason=%r",
            resolved,
            reason,
        )
    except OSError:
        _LOG.exception("Launcher OBS restart spawn failed script=%s", resolved)
