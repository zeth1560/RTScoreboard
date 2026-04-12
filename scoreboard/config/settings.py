"""Application settings: load from environment (.env) with validation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from scoreboard.hotkeys import parse_recording_hotkey_to_tk_bind

_LOG = logging.getLogger(__name__)

# Defaults (formerly module-level constants in main.py)
DEFAULT_STATE_FILE = "state.json"
DEFAULT_ENV_FILE = ".env"
DEFAULT_SCOREBOARD_BG = "Score BG.png"
DEFAULT_REPLAY_SLATE = "ir slate.png"
DEFAULT_SLIDESHOW_DIR = r"C:\Users\admin\Dropbox\slideshow"
DEFAULT_REPLAY_VIDEO_PATH = r"C:\ReplayTrove\INSTANTREPLAY.mp4"

IDLE_TIMEOUT_MS = 30 * 60 * 1000
SLIDESHOW_INTERVAL_MS = 12 * 1000
SLIDESHOW_FADE_DURATION_MS = 1000
SLIDESHOW_FADE_STEPS = 10
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

REPLAY_VIDEO_START_DELAY_MS = 3000
REPLAY_VIDEO_POLL_MS = 500
REPLAY_RETURN_SLATE_HOLD_MS = 350
# If fade or handoff hangs, force recovery (ms)
REPLAY_TRANSITION_TIMEOUT_MS = 90_000
# After slate is shown, if video never becomes active this long after launch delay, recover (ms)
REPLAY_SLATE_STUCK_TIMEOUT_MS = 90_000
FOCUS_WATCHDOG_INTERVAL_MS = 3000
FOCUS_WATCHDOG_TICKS = 45

RECORDING_DEFAULT_DURATION_MINUTES = 20
RECORDING_COUNTDOWN_TICK_MS = 1000
RECORDING_BLINK_INTERVAL_MS = 500
RECORDING_OVERLAY_WIDTH = 440
RECORDING_OVERLAY_HEIGHT = 178
RECORDING_ENDED_MESSAGE = (
    "Your recording has reached its maximum length and ended"
)
RECORDING_ENDED_HOLD_MINUTES_DEFAULT = 2


def _env_truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_positive_int(raw: str | None, default: int, name: str, minimum: int = 1) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        n = int(float(str(raw).strip()))
        if n < minimum:
            _LOG.warning(
                "%s=%r below minimum %s; using default %s",
                name,
                raw,
                minimum,
                default,
            )
            return default
        return n
    except (TypeError, ValueError):
        _LOG.warning("%s=%r invalid; using default %s", name, raw, default)
        return default


def _normalize_path(p: str | None) -> str:
    if p is None:
        return ""
    return str(p).strip().strip('"').strip("'")


@dataclass(frozen=True)
class Settings:
    """Validated configuration loaded once at startup."""

    # Paths
    state_file: str
    scoreboard_background_image: str
    replay_slate_image: str
    slideshow_dir: str
    replay_video_path: str
    mpv_path: str | None

    # mpv / replay
    mpv_exit_hotkey: str
    mpv_embedded: bool

    # Windows focus
    synthetic_focus_click: bool

    # Recording overlay
    recording_max_minutes: int
    recording_duration_sec: int
    recording_ended_hold_ms: int
    recording_start_hotkey: str
    recording_dismiss_hotkey: str
    black_screen_hotkey: str

    # Timing (fixed product defaults; not from .env unless we add later)
    idle_timeout_ms: int = IDLE_TIMEOUT_MS
    slideshow_interval_ms: int = SLIDESHOW_INTERVAL_MS
    slideshow_fade_duration_ms: int = SLIDESHOW_FADE_DURATION_MS
    slideshow_fade_steps: int = SLIDESHOW_FADE_STEPS
    replay_video_start_delay_ms: int = REPLAY_VIDEO_START_DELAY_MS
    replay_video_poll_ms: int = REPLAY_VIDEO_POLL_MS
    replay_return_slate_hold_ms: int = REPLAY_RETURN_SLATE_HOLD_MS
    focus_watchdog_interval_ms: int = FOCUS_WATCHDOG_INTERVAL_MS
    focus_watchdog_ticks: int = FOCUS_WATCHDOG_TICKS
    recording_countdown_tick_ms: int = RECORDING_COUNTDOWN_TICK_MS
    recording_blink_interval_ms: int = RECORDING_BLINK_INTERVAL_MS
    recording_overlay_width: int = RECORDING_OVERLAY_WIDTH
    recording_overlay_height: int = RECORDING_OVERLAY_HEIGHT
    recording_ended_message: str = RECORDING_ENDED_MESSAGE

    # Pilot / reliability
    replay_enabled: bool = True
    slideshow_enabled: bool = True
    scoreboard_debug: bool = False
    heartbeat_interval_minutes: int = 0
    replay_transition_timeout_ms: int = REPLAY_TRANSITION_TIMEOUT_MS
    replay_slate_stuck_timeout_ms: int = REPLAY_SLATE_STUCK_TIMEOUT_MS


def load_settings(env_file: str = DEFAULT_ENV_FILE) -> Settings:
    """Load .env into os.environ, then build and validate Settings."""
    env_path = Path(env_file)
    if env_path.is_file():
        load_dotenv(env_path, override=False)
        _LOG.info("Loaded environment from %s", env_path.resolve())
    else:
        _LOG.info("No %s file; using process environment and defaults", env_file)

    def g(key: str, default: str | None = None) -> str | None:
        v = os.environ.get(key)
        if v is None or str(v).strip() == "":
            return default
        return str(v).strip()

    slideshow_dir = _normalize_path(
        g("SLIDESHOW_DIR", DEFAULT_SLIDESHOW_DIR) or DEFAULT_SLIDESHOW_DIR
    )
    replay_video_path = _normalize_path(
        g("REPLAY_VIDEO_PATH", DEFAULT_REPLAY_VIDEO_PATH) or DEFAULT_REPLAY_VIDEO_PATH
    )
    mpv_path_raw = _normalize_path(g("MPV_PATH"))
    mpv_path = mpv_path_raw if mpv_path_raw else None

    mpv_exit = (g("MPV_EXIT_HOTKEY", "Ctrl+Alt+q") or "Ctrl+Alt+q").strip()
    if not mpv_exit:
        mpv_exit = "Ctrl+Alt+q"

    mpv_embedded = _env_truthy(g("MPV_EMBEDDED"), False)

    syn_default = True if os.name == "nt" else False
    synthetic_focus_click = _env_truthy(g("SYNTHETIC_FOCUS_CLICK"), syn_default)

    rec_minutes = _parse_positive_int(
        g("RECORDING_MAX_MINUTES", str(RECORDING_DEFAULT_DURATION_MINUTES)),
        RECORDING_DEFAULT_DURATION_MINUTES,
        "RECORDING_MAX_MINUTES",
        minimum=1,
    )
    recording_duration_sec = rec_minutes * 60

    ended_hold_min = _parse_positive_int(
        g("RECORDING_ENDED_HOLD_MINUTES", str(RECORDING_ENDED_HOLD_MINUTES_DEFAULT)),
        RECORDING_ENDED_HOLD_MINUTES_DEFAULT,
        "RECORDING_ENDED_HOLD_MINUTES",
        minimum=1,
    )
    recording_ended_hold_ms = ended_hold_min * 60 * 1000

    state_file = _normalize_path(g("STATE_FILE", DEFAULT_STATE_FILE)) or DEFAULT_STATE_FILE
    scoreboard_bg = (
        _normalize_path(g("SCOREBOARD_BACKGROUND_IMAGE", DEFAULT_SCOREBOARD_BG))
        or DEFAULT_SCOREBOARD_BG
    )
    replay_slate = (
        _normalize_path(g("REPLAY_SLATE_IMAGE", DEFAULT_REPLAY_SLATE)) or DEFAULT_REPLAY_SLATE
    )

    recording_start = (
        g("RECORDING_START_HOTKEY", "Ctrl+Shift+g") or "Ctrl+Shift+g"
    ).strip()
    recording_dismiss = (
        g("RECORDING_DISMISS_HOTKEY", "Ctrl+Alt+m") or "Ctrl+Alt+m"
    ).strip()
    black_screen = (g("BLACK_SCREEN_HOTKEY", "Ctrl+Shift+b") or "Ctrl+Shift+b").strip()

    replay_enabled = _env_truthy(g("REPLAY_ENABLED"), True)
    slideshow_enabled = _env_truthy(g("SLIDESHOW_ENABLED"), True)
    scoreboard_debug = _env_truthy(g("SCOREBOARD_DEBUG"), False)

    heartbeat_interval_minutes = _parse_positive_int(
        g("HEARTBEAT_INTERVAL_MINUTES", "0"),
        0,
        "HEARTBEAT_INTERVAL_MINUTES",
        minimum=0,
    )

    transition_timeout = _parse_positive_int(
        g("REPLAY_TRANSITION_TIMEOUT_MS", str(REPLAY_TRANSITION_TIMEOUT_MS)),
        REPLAY_TRANSITION_TIMEOUT_MS,
        "REPLAY_TRANSITION_TIMEOUT_MS",
        minimum=5000,
    )
    slate_stuck_timeout = _parse_positive_int(
        g("REPLAY_SLATE_STUCK_TIMEOUT_MS", str(REPLAY_SLATE_STUCK_TIMEOUT_MS)),
        REPLAY_SLATE_STUCK_TIMEOUT_MS,
        "REPLAY_SLATE_STUCK_TIMEOUT_MS",
        minimum=5000,
    )

    settings = Settings(
        state_file=state_file,
        scoreboard_background_image=scoreboard_bg,
        replay_slate_image=replay_slate,
        slideshow_dir=slideshow_dir,
        replay_video_path=replay_video_path,
        mpv_path=mpv_path,
        mpv_exit_hotkey=mpv_exit,
        mpv_embedded=mpv_embedded,
        synthetic_focus_click=synthetic_focus_click,
        recording_max_minutes=rec_minutes,
        recording_duration_sec=recording_duration_sec,
        recording_ended_hold_ms=recording_ended_hold_ms,
        recording_start_hotkey=recording_start,
        recording_dismiss_hotkey=recording_dismiss,
        black_screen_hotkey=black_screen,
        replay_enabled=replay_enabled,
        slideshow_enabled=slideshow_enabled,
        scoreboard_debug=scoreboard_debug,
        heartbeat_interval_minutes=heartbeat_interval_minutes,
        replay_transition_timeout_ms=transition_timeout,
        replay_slate_stuck_timeout_ms=slate_stuck_timeout,
    )

    _validate_hotkey_specs(settings)
    _validate_timing_sane(settings)
    return settings


def _validate_timing_sane(settings: Settings) -> None:
    if settings.idle_timeout_ms < 1000:
        _LOG.warning("idle_timeout_ms=%s is very low", settings.idle_timeout_ms)
    if settings.recording_duration_sec < 60:
        _LOG.warning("recording duration under 1 minute may be unintended")
    if settings.slideshow_fade_steps < 1:
        _LOG.error("slideshow_fade_steps invalid; check defaults")
    if settings.replay_video_start_delay_ms < 0:
        _LOG.error("replay_video_start_delay_ms must be non-negative")


def _validate_hotkey_specs(settings: Settings) -> None:
    for name, spec, fallback in (
        ("RECORDING_START_HOTKEY", settings.recording_start_hotkey, "Ctrl+Shift+g"),
        ("RECORDING_DISMISS_HOTKEY", settings.recording_dismiss_hotkey, "Ctrl+Alt+m"),
        ("BLACK_SCREEN_HOTKEY", settings.black_screen_hotkey, "Ctrl+Shift+b"),
    ):
        if parse_recording_hotkey_to_tk_bind(spec) is None:
            _LOG.warning(
                "%s=%r is not a valid chord; binding will try %r",
                name,
                spec,
                fallback,
            )


def summarize_settings(settings: Settings) -> str:
    """Human-readable summary for startup diagnostics (no secrets)."""
    lines = [
        f"state_file={settings.state_file!r}",
        f"scoreboard_background_image={settings.scoreboard_background_image!r}",
        f"replay_slate_image={settings.replay_slate_image!r}",
        f"slideshow_dir={settings.slideshow_dir!r}",
        f"replay_video_path={settings.replay_video_path!r}",
        f"mpv_path={settings.mpv_path!r}",
        f"mpv_embedded={settings.mpv_embedded}",
        f"mpv_exit_hotkey={settings.mpv_exit_hotkey!r}",
        f"synthetic_focus_click={settings.synthetic_focus_click}",
        f"recording_max_minutes={settings.recording_max_minutes}",
        f"recording_ended_hold_ms={settings.recording_ended_hold_ms}",
        f"recording_start_hotkey={settings.recording_start_hotkey!r}",
        f"recording_dismiss_hotkey={settings.recording_dismiss_hotkey!r}",
        f"black_screen_hotkey={settings.black_screen_hotkey!r}",
        f"idle_timeout_ms={settings.idle_timeout_ms}",
        f"slideshow_interval_ms={settings.slideshow_interval_ms}",
        f"replay_enabled={settings.replay_enabled}",
        f"slideshow_enabled={settings.slideshow_enabled}",
        f"scoreboard_debug={settings.scoreboard_debug}",
        f"heartbeat_interval_minutes={settings.heartbeat_interval_minutes}",
        f"replay_transition_timeout_ms={settings.replay_transition_timeout_ms}",
        f"replay_slate_stuck_timeout_ms={settings.replay_slate_stuck_timeout_ms}",
    ]
    return "\n".join(lines)


