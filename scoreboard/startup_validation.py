"""Startup checks: fail-fast for critical assets, pilot diagnostics summary."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from scoreboard.config.settings import Settings, SUPPORTED_IMAGE_EXTENSIONS, summarize_settings
from scoreboard.obs_restart import resolve_obs_executable
from scoreboard.hotkeys import parse_recording_hotkey_to_tk_bind
from scoreboard.version import __version__

_LOG = logging.getLogger(__name__)


def _mpv_candidates(settings: Settings) -> list[str]:
    candidates: list[str] = []
    if settings.mpv_path:
        candidates.append(settings.mpv_path)
    discovered = shutil.which("mpv")
    if discovered:
        candidates.append(discovered)
    discovered_exe = shutil.which("mpv.exe")
    if discovered_exe:
        candidates.append(discovered_exe)
    candidates.extend(
        [
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Program Files (x86)\mpv\mpv.exe",
            r"C:\mpv\mpv.exe",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_mpv_executable(settings: Settings) -> str | None:
    for candidate in _mpv_candidates(settings):
        if os.path.isfile(candidate):
            return candidate
    return None


def count_slideshow_images(settings: Settings) -> int:
    d = settings.slideshow_dir
    if not d or not os.path.isdir(d):
        return 0
    n = 0
    try:
        for name in os.listdir(d):
            if name.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    n += 1
    except OSError:
        _LOG.exception("Could not enumerate slideshow images in %s", d)
        return 0
    return n


def validate_startup_critical(settings: Settings) -> None:
    """
    Fail fast before building UI if required assets are missing.
    Replay video and mpv are required only when replay_enabled.
    Slideshow directory is required only when slideshow_enabled (may be empty).
    """
    errors: list[str] = []

    bg = Path(settings.scoreboard_background_image)
    if not bg.is_file():
        errors.append(f"Scoreboard background missing (required): {bg.resolve()}")

    slate = Path(settings.replay_slate_image)
    if not slate.is_file():
        errors.append(f"Replay slate image missing (required): {slate.resolve()}")

    if settings.replay_enabled:
        rv = Path(settings.replay_video_path)
        if not rv.is_file():
            errors.append(
                f"Replay video missing but REPLAY_ENABLED=1: {rv.resolve()} "
                f"(set REPLAY_ENABLED=0 for scoreboard-only pilot, or fix path)"
            )
        mpv = resolve_mpv_executable(settings)
        if mpv is None:
            errors.append(
                "mpv executable not found but REPLAY_ENABLED=1 "
                "(install mpv, set MPV_PATH, or set REPLAY_ENABLED=0)"
            )

    if settings.slideshow_enabled:
        sd = Path(settings.slideshow_dir)
        if not sd.is_dir():
            errors.append(
                f"Slideshow directory missing but SLIDESHOW_ENABLED=1: {sd.resolve()} "
                f"(create it or set SLIDESHOW_ENABLED=0)"
            )

    state_path = Path(settings.state_file)
    if state_path.is_file():
        try:
            state_path.read_text(encoding="utf-8")
        except OSError:
            _LOG.exception("State file unreadable (will reset on load): %s", state_path)

    for name, spec, default in (
        ("RECORDING_START_HOTKEY", settings.recording_start_hotkey, "Ctrl+Shift+g"),
        ("RECORDING_DISMISS_HOTKEY", settings.recording_dismiss_hotkey, "Ctrl+Alt+m"),
        ("BLACK_SCREEN_HOTKEY", settings.black_screen_hotkey, "Ctrl+Shift+b"),
    ):
        if parse_recording_hotkey_to_tk_bind(spec) is None:
            _LOG.warning(
                "%s=%r invalid; binding will fall back toward %r at runtime",
                name,
                spec,
                default,
            )

    if errors:
        for msg in errors:
            _LOG.error("Startup validation failed: %s", msg)
        _LOG.error("Exiting: fix configuration or disable optional features via .env")
        sys.exit(1)


def log_pilot_diagnostics_summary(
    settings: Settings,
    *,
    screen_width: int,
    screen_height: int,
) -> None:
    """Concise operator-facing startup block for long-run pilot observation."""
    mpv = resolve_mpv_executable(settings) if settings.replay_enabled else None
    n_img = count_slideshow_images(settings) if settings.slideshow_enabled else 0

    loading_dir = Path(settings.replay_buffer_loading_dir)
    loading_frames_ok = all(
        (loading_dir / f"Loading{i:02d}.png").is_file() for i in range(1, 12)
    )

    hotkey_lines = []
    for label, spec in (
        ("record_start", settings.recording_start_hotkey),
        ("record_dismiss", settings.recording_dismiss_hotkey),
        ("black_screen", settings.black_screen_hotkey),
        ("replay_buffer_loading", settings.replay_buffer_loading_hotkey),
    ):
        p = parse_recording_hotkey_to_tk_bind(spec)
        hotkey_lines.append(f"    {label}: {spec!r} -> {p!r}")

    st = Path(settings.state_file)
    state_note = "present" if st.is_file() else "absent (will create on save)"

    obs_dep_note = "n/a"
    if settings.recording_obs_health_check:
        try:
            import obsws_python  # noqa: F401
            obs_dep_note = "obsws-python=OK"
        except ImportError:
            obs_dep_note = "obsws-python=MISSING"

    obs_restart_note = "off"
    if settings.obs_restart_chord_enabled:
        exe = resolve_obs_executable(settings)
        obs_restart_note = (
            f"on exe={'OK ' + exe if exe else 'MISSING (set OBS_EXECUTABLE)'}"
        )

    summary = (
        f"PILOT STARTUP DIAGNOSTICS\n"
        f"  app_version={__version__}\n"
        f"  screen={screen_width}x{screen_height}\n"
        f"  state_file={settings.state_file!r} ({state_note})\n"
        f"  replay_enabled={settings.replay_enabled} "
        f"mpv={'OK ' + mpv if mpv else 'MISSING'}\n"
        f"  replay_video={'OK' if Path(settings.replay_video_path).is_file() else 'MISSING'} "
        f"path={settings.replay_video_path!r}\n"
        f"  replay_unavailable_image="
        f"{'OK' if Path(settings.replay_unavailable_image).is_file() else 'MISSING'} "
        f"path={settings.replay_unavailable_image!r}\n"
        f"  replay_buffer_loading_frames="
        f"{'OK' if loading_frames_ok else 'MISSING'} "
        f"dir={settings.replay_buffer_loading_dir!r}\n"
        f"  encoder_status_overlay="
        f"{'on' if settings.encoder_status_enabled else 'off'} "
        f"json={'OK' if Path(settings.encoder_state_path).is_file() else 'absent'} "
        f"ready_png={'OK' if Path(settings.encoder_status_ready_image).is_file() else 'MISSING'} "
        f"unavail_png={'OK' if Path(settings.encoder_status_unavailable_image).is_file() else 'MISSING'}\n"
        f"  slideshow_enabled={settings.slideshow_enabled} "
        f"dir_ok={Path(settings.slideshow_dir).is_dir() if settings.slideshow_enabled else 'n/a'} "
        f"image_count={n_img}\n"
        f"  recording_max_minutes={settings.recording_max_minutes} "
        f"idle_timeout_min={settings.idle_timeout_ms // 60000}\n"
        f"  heartbeat_interval_minutes={settings.heartbeat_interval_minutes} "
        f"(0=off)\n"
        f"  recording_obs_health_check={settings.recording_obs_health_check} "
        f"ws={settings.obs_websocket_host!r}:{settings.obs_websocket_port} "
        f"timeout_sec={settings.obs_websocket_timeout_sec} {obs_dep_note}\n"
        f"  obs_restart_chord={obs_restart_note}\n"
        f"  obs_status_indicator={settings.obs_status_indicator_enabled} "
        f"poll_ms={settings.obs_status_poll_interval_ms} "
        f"require_main_idle={settings.obs_status_require_main_output_idle}\n"
        f"  scoreboard_debug={settings.scoreboard_debug}\n"
        f"  hotkeys:\n"
        + "\n".join(hotkey_lines)
    )
    _LOG.info("%s", summary)
    _LOG.info("Full settings snapshot:\n%s", summarize_settings(settings))


def validate_screen_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        _LOG.error("Invalid screen dimensions %sx%s; cannot continue", width, height)
        sys.exit(1)


def log_startup_validation(settings: Settings, state_path: Path) -> None:
    """Backward-compatible name: log readable state file (critical checks run earlier)."""
    if state_path.is_file():
        try:
            state_path.read_text(encoding="utf-8")
            _LOG.info("State file readable: %s", state_path.resolve())
        except OSError:
            _LOG.exception("State file not readable: %s", state_path.resolve())
    else:
        _LOG.info("State file does not exist yet: %s", state_path.resolve())
