"""Replay slate fades, mpv launch/embed/fullscreen, polling, recovery, and teardown."""

from __future__ import annotations

import enum
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from typing import Callable

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings
from scoreboard.launcher_obs_restart import request_launcher_obs_restart
from scoreboard.obs_health import notify_obs_instant_replay_unavailable
from scoreboard.scheduler import AfterScheduler

_LOG = logging.getLogger(__name__)

# Fallback if REPLAY_UNAVAILABLE_IMAGE is missing or unreadable (logs stay specific).
REPLAY_UNAVAILABLE_USER_MESSAGE = (
    "Sorry…the squirrels that make this contraption work stayed up partying way too late "
    "last night, and they are all currently either asleep, hungover, or puking in the "
    "restroom. We will give them a stern talking to, and hopefully have better luck with "
    "this doohickey next time."
)

# Share main canvas with the scoreboard (Toplevels tend to flash under Windows DWM on repaint).
_REPLAY_UNAVAIL_CANVAS_TAG = "replay_unavailable"
# Relative to "fit entire image on screen" scale (centered, letterboxed by transparent overlay).
_REPLAY_UNAVAILABLE_DISPLAY_SCALE = 0.5


class ReplayPhase(enum.Enum):
    IDLE = enum.auto()
    FADING_IN = enum.auto()
    SLATE_VISIBLE = enum.auto()
    VIDEO_PLAYING = enum.auto()
    FADING_OUT = enum.auto()


class ReplayController:
    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        scheduler: AfterScheduler,
        canvas: tk.Canvas,
        video_host: tk.Frame,
        bg_canvas_id: int,
        overlay_canvas_id: int,
        replay_image_rgba: Image.Image,
        transparent_overlay_photo: ImageTk.PhotoImage,
        lift_recording_overlay: Callable[[], None],
        before_slate_fade_in: Callable[[], None],
        after_replay_fade_out: Callable[[], None],
        redraw_scores: Callable[[], None],
        after_overlay_raise: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._settings = settings
        self._scheduler = scheduler
        self._canvas = canvas
        self._video_host = video_host
        self._bg_canvas_id = bg_canvas_id
        self._overlay_canvas_id = overlay_canvas_id
        self._replay_image = replay_image_rgba
        self._transparent_overlay_photo = transparent_overlay_photo
        self._lift_recording_overlay = lift_recording_overlay
        self._before_slate_fade_in = before_slate_fade_in
        self._after_replay_fade_out = after_replay_fade_out
        self._redraw_scores = redraw_scores
        self._after_overlay_raise = after_overlay_raise

        self._phase = ReplayPhase.IDLE
        self._showing_replay = False
        self._is_transitioning = False
        self._replay_video_active = False
        self._video_host_visible = False
        self._replay_video_process: subprocess.Popen | None = None
        self._mpv_input_conf_path: str | None = None

        self._start_job: str | None = None
        self._embed_spawn_job: str | None = None
        self._poll_job: str | None = None
        self._overlay_fade_job: str | None = None
        self._return_slate_job: str | None = None
        self._transition_timeout_job: str | None = None
        self._slate_stuck_job: str | None = None

        self._fade_frames: list[ImageTk.PhotoImage] = []
        self._current_overlay_photo: ImageTk.PhotoImage = transparent_overlay_photo
        # Precompute overlay fade frames across after() ticks so the Tk thread is not frozen
        # (full-screen PIL + PhotoImage work on 1440p/4K can take multiple seconds if done at once).
        self._fade_build_active = False
        self._fade_build_steps = 0
        self._fade_build_start_alpha = 0
        self._fade_build_end_alpha = 0
        self._fade_anim_delay_ms = 0
        self._fade_on_complete: Callable[[], None] | None = None

        self._replay_unavail_photo: ImageTk.PhotoImage | None = None
        self._last_replayed_file_mtime: float | None = None
        self._replay_pending_mtime: float | None = None

    @property
    def phase(self) -> ReplayPhase:
        return self._phase

    @property
    def showing_replay(self) -> bool:
        return self._showing_replay

    @property
    def is_transitioning(self) -> bool:
        return self._is_transitioning

    @property
    def replay_video_active(self) -> bool:
        return self._replay_video_active

    def _set_phase(self, p: ReplayPhase) -> None:
        if p != self._phase:
            _LOG.info("Replay phase: %s -> %s", self._phase.name, p.name)
        self._phase = p

    def _raise_fullscreen_overlay(self) -> None:
        self._canvas.tag_raise(self._overlay_canvas_id)
        if self._after_overlay_raise is not None:
            self._after_overlay_raise()

    def blocks_idle(self) -> bool:
        return self._showing_replay or self._is_transitioning

    def blocks_black_screen_toggle(self) -> bool:
        return (
            self._is_transitioning
            or self._replay_video_active
            or self._showing_replay
        )

    def blocks_score_updates(self) -> bool:
        return self._showing_replay or self._is_transitioning

    def current_overlay_photo_ref(self) -> ImageTk.PhotoImage:
        return self._current_overlay_photo

    def set_current_overlay_photo_ref(self, photo: ImageTk.PhotoImage) -> None:
        self._current_overlay_photo = photo

    def cancel_overlay_fade(self) -> None:
        self._fade_build_active = False
        self._fade_on_complete = None
        self._scheduler.cancel(self._overlay_fade_job)
        self._overlay_fade_job = None
        self._fade_frames.clear()

    def cancel_replay_video_launch(self) -> None:
        self._scheduler.cancel(self._start_job)
        self._start_job = None
        self._scheduler.cancel(self._embed_spawn_job)
        self._embed_spawn_job = None

    def cancel_replay_video_poll(self) -> None:
        self._scheduler.cancel(self._poll_job)
        self._poll_job = None

    def cancel_return_slate(self) -> None:
        self._scheduler.cancel(self._return_slate_job)
        self._return_slate_job = None

    def cancel_transition_timeout(self) -> None:
        self._scheduler.cancel(self._transition_timeout_job)
        self._transition_timeout_job = None

    def cancel_slate_stuck_watchdog(self) -> None:
        self._scheduler.cancel(self._slate_stuck_job)
        self._slate_stuck_job = None

    def _cancel_all_replay_timers(self) -> None:
        self.cancel_overlay_fade()
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()
        self.cancel_return_slate()
        self.cancel_transition_timeout()
        self.cancel_slate_stuck_watchdog()

    def _apply_idle_scoreboard_state(self, reason: str, *, run_after_hook: bool = True) -> None:
        """Transparent overlay, scores visible, replay flags IDLE — shared finalize path."""
        self.hide_video_host()
        # Switch the canvas overlay to transparent *before* draw_scores / aux sync so the
        # encoder strip is not briefly tag-raised above a full-frame slate/replay image.
        self._current_overlay_photo = self._transparent_overlay_photo
        try:
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._transparent_overlay_photo,
            )
        except tk.TclError:
            _LOG.exception("Replay restore: overlay image failed (reason=%s)", reason)
        try:
            if self._root.winfo_exists():
                self.show_canvas_after_video()
                self.restore_canvas_after_video()
        except tk.TclError:
            _LOG.exception("Replay restore: canvas layout/restore failed (reason=%s)", reason)
        self._raise_fullscreen_overlay()
        self._showing_replay = False
        self._replay_video_active = False
        self._is_transitioning = False
        self._set_phase(ReplayPhase.IDLE)
        self._cleanup_mpv_input_conf()
        if run_after_hook:
            try:
                self._after_replay_fade_out()
            except Exception:
                _LOG.exception("Replay restore: after_replay_fade_out failed (reason=%s)", reason)

    def restore_normal_scoreboard_state(self, reason: str, *, log_level: int = logging.WARNING) -> None:
        """
        Single known-good path: scores visible, transparent overlay, replay flags cleared.
        Safe to call after launch failure, stuck watchdog, or partial teardown.
        """
        _LOG.log(log_level, "Replay: restoring normal scoreboard (reason=%s)", reason)
        self._replay_pending_mtime = None
        self._cancel_all_replay_timers()
        self.stop_replay_video_process()
        self._apply_idle_scoreboard_state(reason, run_after_hook=True)
        _LOG.info("Replay: scoreboard restored to normal mode (reason=%s)", reason)

    def _snap_idle_for_replay_unavailable(self, reason: str, *, log_level: int = logging.INFO) -> None:
        """Cancel replay work and show the scoreboard under the unavailable graphic.

        Skips ``_after_replay_fade_out`` so this is not treated as a normal replay exit transition.
        """
        _LOG.log(log_level, "Replay: idle snap for unavailable graphic (reason=%s)", reason)
        self._replay_pending_mtime = None
        self._cancel_all_replay_timers()
        self.stop_replay_video_process()
        self._apply_idle_scoreboard_state(reason, run_after_hook=False)

    def _present_slate_after_mpv_stopped(self) -> None:
        """Shared canvas restore when mpv exits or operator stops video (hold slate before fade-out)."""
        self.hide_video_host()
        self.show_canvas_after_video()
        self.restore_canvas_after_video()
        if not self._settings.mpv_embedded:
            self._current_overlay_photo = ImageTk.PhotoImage(self._replay_image)
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._current_overlay_photo,
            )
        self._raise_fullscreen_overlay()
        self._root.update_idletasks()
        self._set_phase(ReplayPhase.SLATE_VISIBLE)

    def _schedule_return_to_scoreboard_fade(self, hold_ms: int, *, source: str) -> None:
        """Single entry point for hold-then-fade-out (avoids duplicate return_slate scheduling)."""
        self.cancel_return_slate()
        self._return_slate_job = self._scheduler.schedule(
            hold_ms,
            self._fade_overlay_out,
            name=f"replay_return_fade_hold:{source}",
        )

    def _schedule_transition_watchdog(self, phase_label: str) -> None:
        self.cancel_transition_timeout()
        ms = self._settings.replay_transition_timeout_ms
        self._transition_timeout_job = self._scheduler.schedule(
            ms,
            lambda: self._on_transition_timeout(phase_label),
            name=f"replay_transition_timeout:{phase_label}",
        )

    def _on_transition_timeout(self, phase_label: str) -> None:
        self._transition_timeout_job = None
        if not self._is_transitioning:
            return
        _LOG.error(
            "Replay stuck in transition (%s) after %s ms; forcing recovery",
            phase_label,
            self._settings.replay_transition_timeout_ms,
        )
        self._snap_idle_for_replay_unavailable(
            f"transition_timeout:{phase_label}",
            log_level=logging.ERROR,
        )
        self._show_replay_unavailable_toast()

    def _schedule_slate_stuck_watchdog(self) -> None:
        self.cancel_slate_stuck_watchdog()
        delay = (
            self._settings.replay_video_start_delay_ms
            + self._settings.replay_slate_stuck_timeout_ms
        )
        self._slate_stuck_job = self._scheduler.schedule(
            delay,
            self._on_slate_stuck_timeout,
            name="replay_slate_stuck_watchdog",
        )

    def _on_slate_stuck_timeout(self) -> None:
        self._slate_stuck_job = None
        if not self._showing_replay or self._is_transitioning:
            return
        if self._replay_video_active:
            return
        _LOG.error(
            "Replay: slate visible but video never started after %s ms; forcing recovery",
            self._settings.replay_video_start_delay_ms
            + self._settings.replay_slate_stuck_timeout_ms,
        )
        self._snap_idle_for_replay_unavailable("slate_stuck_no_video", log_level=logging.ERROR)
        self._show_replay_unavailable_toast()

    def teardown(self) -> None:
        _LOG.info("Replay: teardown")
        self._dismiss_replay_unavailable_toast()
        self._last_replayed_file_mtime = None
        self._replay_pending_mtime = None
        self._cancel_all_replay_timers()
        self.stop_replay_video_process()
        self.hide_video_host()
        try:
            if self._root.winfo_exists():
                self.show_canvas_after_video()
                self.restore_canvas_after_video()
        except tk.TclError:
            _LOG.warning("Replay teardown: canvas restore failed", exc_info=True)
        self._fade_frames.clear()
        self._showing_replay = False
        self._replay_video_active = False
        self._is_transitioning = False
        self._set_phase(ReplayPhase.IDLE)
        self._current_overlay_photo = self._transparent_overlay_photo
        try:
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._transparent_overlay_photo,
            )
        except tk.TclError:
            _LOG.debug("Replay teardown: overlay reset skipped", exc_info=True)
        self._cleanup_mpv_input_conf()

    def sync_replay_unavailable_canvas_stack(self) -> None:
        """Keep the unavailable graphic above the transparent overlay and auxiliary strips."""
        if not self._canvas.find_withtag(_REPLAY_UNAVAIL_CANVAS_TAG):
            return
        try:
            for ref in (self._overlay_canvas_id, "encoder_status", "replay_buffer_loading"):
                try:
                    self._canvas.tag_raise(_REPLAY_UNAVAIL_CANVAS_TAG, ref)
                except tk.TclError:
                    pass
        except tk.TclError:
            _LOG.debug("replay unavailable: tag_raise failed", exc_info=True)

    def _dismiss_replay_unavailable_toast(self) -> None:
        try:
            self._canvas.delete(_REPLAY_UNAVAIL_CANVAS_TAG)
        except tk.TclError:
            pass
        self._replay_unavail_photo = None

    def dismiss_replay_unavailable_overlay(self) -> bool:
        """Remove only the unavailable canvas graphic.

        Replay is already snapped idle when this graphic was shown; no fade-out or extra restore.
        """
        if not self._canvas.find_withtag(_REPLAY_UNAVAIL_CANVAS_TAG):
            return False
        self._dismiss_replay_unavailable_toast()
        return True

    def show_replay_unavailable_graphic_overlay(self) -> None:
        """Same fullscreen asset as failed replay (``REPLAY_UNAVAILABLE_IMAGE`` / fallback text)."""
        self._show_replay_unavailable_toast()

    def _try_build_replay_unavailable_photo(self) -> tuple[ImageTk.PhotoImage, int, int] | None:
        path = self._settings.replay_unavailable_image
        if not path or not os.path.isfile(path):
            _LOG.warning("Replay unavailable image missing: %s", path)
            return None
        try:
            sw = max(1, self._root.winfo_screenwidth())
            sh = max(1, self._root.winfo_screenheight())
            with Image.open(path) as img:
                rgba = img.convert("RGBA")
                iw, ih = rgba.size
                fit = min(sw / iw, sh / ih)
                scale = fit * _REPLAY_UNAVAILABLE_DISPLAY_SCALE
                nw = max(1, int(iw * scale))
                nh = max(1, int(ih * scale))
                if (nw, nh) != (iw, ih):
                    rgba = rgba.resize((nw, nh), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(rgba)
            return (photo, nw, nh)
        except OSError:
            _LOG.exception("Replay unavailable image load failed: %s", path)
            return None

    def _show_replay_unavailable_toast(self) -> None:
        """Centered graphic on the main canvas; dismiss via mpv exit hotkey / replay stop / ``i`` when wired in app."""
        if self._canvas.find_withtag(_REPLAY_UNAVAIL_CANVAS_TAG):
            self._raise_fullscreen_overlay()
            try:
                self._lift_recording_overlay()
            except Exception:
                _LOG.debug("replay unavailable lift_recording_overlay failed", exc_info=True)
            return
        sw = max(1, self._root.winfo_screenwidth())
        sh = max(1, self._root.winfo_screenheight())
        try:
            built = self._try_build_replay_unavailable_photo()
            if built is not None:
                photo, _nw, _nh = built
                self._replay_unavail_photo = photo
                self._canvas.create_image(
                    sw // 2,
                    sh // 2,
                    anchor="center",
                    image=photo,
                    tags=_REPLAY_UNAVAIL_CANVAS_TAG,
                )
            else:
                wrap = min(960, max(320, sw - 120))
                self._canvas.create_text(
                    sw // 2,
                    sh // 2,
                    text=REPLAY_UNAVAILABLE_USER_MESSAGE,
                    fill="#f0f0f0",
                    font=("Arial", 14),
                    width=wrap,
                    justify="center",
                    tags=_REPLAY_UNAVAIL_CANVAS_TAG,
                )
        except tk.TclError:
            _LOG.debug("replay unavailable overlay failed", exc_info=True)
            return

        self._raise_fullscreen_overlay()
        try:
            self._lift_recording_overlay()
        except Exception:
            _LOG.debug("replay unavailable lift_recording_overlay failed", exc_info=True)

    def _commit_replay_file_played(self) -> None:
        if self._replay_pending_mtime is not None:
            self._last_replayed_file_mtime = self._replay_pending_mtime
            self._replay_pending_mtime = None

    def start_replay_session(self) -> None:
        """Begin instant replay (fade to slate) from idle — not a toggle; use ``stop_replay_session`` to exit."""
        if not self._settings.replay_enabled:
            _LOG.info("Replay: start ignored (REPLAY_ENABLED=0)")
            return
        if self._canvas.find_withtag(_REPLAY_UNAVAIL_CANVAS_TAG):
            _LOG.info(
                "Replay: start ignored (failure graphic visible; press %s to dismiss)",
                self._settings.replay_stop_hotkey,
            )
            return
        if self._is_transitioning:
            _LOG.info(
                "Replay: start ignored (transition in progress phase=%s)",
                self._phase.name,
            )
            return
        if self._replay_video_active or self._showing_replay:
            _LOG.info(
                "Replay: start ignored (replay already active; press %s to stop)",
                self._settings.replay_stop_hotkey,
            )
            return
        _LOG.info("Replay: start (fade in to slate)")
        self._fade_overlay_in()

    def stop_replay_session(self) -> None:
        """End replay: stop mpv / fade out slate. Does not remove the failure graphic — app calls dismiss first."""
        if not self._settings.replay_enabled:
            _LOG.info("Replay: stop ignored (REPLAY_ENABLED=0)")
            return
        if self._is_transitioning:
            _LOG.info(
                "Replay: stop ignored (transition in progress phase=%s)",
                self._phase.name,
            )
            return
        _LOG.info(
            "Replay: stop showing=%s video_active=%s phase=%s",
            self._showing_replay,
            self._replay_video_active,
            self._phase.name,
        )
        if self._replay_video_active:
            self.stop_replay_video_and_return()
            return
        if self._showing_replay:
            self.cancel_replay_video_launch()
            self.cancel_return_slate()
            self._fade_overlay_out()

    def _fade_overlay_in(self) -> None:
        if self._phase != ReplayPhase.IDLE:
            _LOG.info("Replay: fade-in ignored (phase=%s)", self._phase.name)
            return
        if self._showing_replay:
            _LOG.info("Replay: fade-in ignored (replay UI already active)")
            return
        _LOG.info("Replay: fade-in to slate starting")
        self._before_slate_fade_in()
        self._set_phase(ReplayPhase.FADING_IN)
        self._is_transitioning = True
        self._schedule_transition_watchdog("fade_in")
        self._run_overlay_fade(
            start_alpha=0,
            end_alpha=255,
            steps=8,
            delay=15,
            on_complete=self._finish_fade_in,
        )

    def _finish_fade_in(self) -> None:
        self.cancel_transition_timeout()
        self._showing_replay = True
        self._is_transitioning = False
        self._set_phase(ReplayPhase.SLATE_VISIBLE)
        _LOG.info("Replay: slate visible; scheduling video launch")
        self._schedule_replay_video_launch()
        self._schedule_slate_stuck_watchdog()

    def _fade_overlay_out(self) -> None:
        if self._phase == ReplayPhase.FADING_OUT:
            _LOG.info("Replay: fade-out ignored (already fading out)")
            return
        if self._phase in (ReplayPhase.FADING_IN, ReplayPhase.VIDEO_PLAYING):
            _LOG.info("Replay: fade-out ignored (phase=%s)", self._phase.name)
            return
        if self._phase == ReplayPhase.IDLE:
            _LOG.info("Replay: fade-out ignored (already idle)")
            return
        _LOG.info("Replay: fade-out from slate starting")
        self.cancel_return_slate()
        self.cancel_slate_stuck_watchdog()
        self._set_phase(ReplayPhase.FADING_OUT)
        self._is_transitioning = True
        self._schedule_transition_watchdog("fade_out")
        self._run_overlay_fade(
            start_alpha=255,
            end_alpha=0,
            steps=10,
            delay=20,
            on_complete=self._finish_fade_out,
        )

    def _finish_fade_out(self) -> None:
        self.cancel_transition_timeout()
        self.cancel_slate_stuck_watchdog()
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()
        self.cancel_return_slate()
        self.stop_replay_video_process()
        self._apply_idle_scoreboard_state("user_replay_fade_out")
        _LOG.info("Replay: fade-out complete; normal scoreboard (user dismiss)")

    def _run_overlay_fade(
        self,
        start_alpha: int,
        end_alpha: int,
        steps: int,
        delay: int,
        on_complete: Callable[[], None],
    ) -> None:
        self.cancel_overlay_fade()
        self._fade_frames = []
        safe_steps = max(1, steps)
        self._fade_build_active = True
        self._fade_build_steps = safe_steps
        self._fade_build_start_alpha = start_alpha
        self._fade_build_end_alpha = end_alpha
        self._fade_anim_delay_ms = delay
        self._fade_on_complete = on_complete
        self._overlay_fade_job = self._scheduler.schedule(
            0,
            self._continue_overlay_fade_build,
            name="replay_overlay_fade_build",
        )

    def _continue_overlay_fade_build(self) -> None:
        """Build one fade frame per scheduler tick; keeps the UI responsive on large displays."""
        self._overlay_fade_job = None
        if not self._fade_build_active:
            return
        steps = self._fade_build_steps
        i = len(self._fade_frames)
        if i > steps:
            self._fade_build_active = False
            on_complete = self._fade_on_complete
            self._fade_on_complete = None
            if on_complete is None:
                return
            self._animate_overlay_fade(0, self._fade_anim_delay_ms, on_complete)
            return
        t = i / steps
        alpha = int(
            self._fade_build_start_alpha
            + (self._fade_build_end_alpha - self._fade_build_start_alpha) * t
        )
        frame = self._replay_image.copy()
        frame.putalpha(alpha)
        photo = ImageTk.PhotoImage(frame)
        self._fade_frames.append(photo)
        self._overlay_fade_job = self._scheduler.schedule(
            1,
            self._continue_overlay_fade_build,
            name="replay_overlay_fade_build",
        )

    def _animate_overlay_fade(
        self,
        index: int,
        delay: int,
        on_complete: Callable[[], None],
    ) -> None:
        if index >= len(self._fade_frames):
            self._fade_frames.clear()
            self._overlay_fade_job = None
            on_complete()
            return

        self._current_overlay_photo = self._fade_frames[index]
        self._canvas.itemconfig(
            self._overlay_canvas_id,
            image=self._current_overlay_photo,
        )
        self._raise_fullscreen_overlay()
        self._lift_recording_overlay()

        self._overlay_fade_job = self._scheduler.schedule(
            delay,
            lambda: self._animate_overlay_fade(index + 1, delay, on_complete),
            name="replay_overlay_fade",
        )

    def _schedule_replay_video_launch(self) -> None:
        self.cancel_replay_video_launch()
        _LOG.info(
            "Replay: launch attempt scheduled in %s ms",
            self._settings.replay_video_start_delay_ms,
        )
        self._start_job = self._scheduler.schedule(
            self._settings.replay_video_start_delay_ms,
            self._start_replay_video,
            name="replay_video_launch_delay",
        )

    def _notify_obs_instant_replay_unavailable_async(self, reason: str) -> None:
        """OBS WebSocket BroadcastCustomEvent; background thread so the UI thread stays responsive."""
        if not self._settings.replay_obs_broadcast_on_unavailable:
            return
        settings = self._settings
        threading.Thread(
            target=notify_obs_instant_replay_unavailable,
            args=(settings, reason),
            daemon=True,
            name="obs-instant-replay-unavailable",
        ).start()

    def _request_launcher_obs_restart_async(self, reason: str) -> None:
        """Run ``C:\\ReplayTrove\\launcher\\restart_obs.ps1`` (or configured path) in a worker thread."""
        if not self._settings.replay_launcher_restart_obs_on_unavailable:
            return
        settings = self._settings
        threading.Thread(
            target=request_launcher_obs_restart,
            args=(settings, reason),
            daemon=True,
            name="launcher-restart-obs",
        ).start()

    def _start_replay_video(self) -> None:
        self._start_job = None

        if not self._showing_replay or self._replay_video_active:
            _LOG.info(
                "Replay: launch skipped (invalid state showing=%s active=%s)",
                self._showing_replay,
                self._replay_video_active,
            )
            return

        path = self._settings.replay_video_path
        exists = bool(path and os.path.isfile(path))
        size_bytes = 0
        mtime_iso = "n/a"
        mtime_epoch: float | None = None
        age_sec: float | None = None
        if exists:
            try:
                st = os.stat(path)
                size_bytes = int(st.st_size)
                mtime_epoch = float(st.st_mtime)
                mtime_iso = datetime.fromtimestamp(st.st_mtime).isoformat(
                    sep="T",
                    timespec="seconds",
                )
                age_sec = max(0.0, time.time() - st.st_mtime)
            except OSError:
                exists = False
                size_bytes = 0
                mtime_iso = "n/a"
                mtime_epoch = None
                age_sec = None
                _LOG.warning("Replay file stat failed for %s", path)

        _LOG.info(
            "Replay file check: exists=%s size_bytes=%s mtime=%s",
            exists,
            size_bytes,
            mtime_iso,
        )

        if not exists:
            _LOG.warning("Replay unavailable: no fresh replay file detected")
            self._notify_obs_instant_replay_unavailable_async("missing_file")
            self._request_launcher_obs_restart_async("missing_file")
            self._snap_idle_for_replay_unavailable("missing_video_file", log_level=logging.WARNING)
            self._show_replay_unavailable_toast()
            return

        if size_bytes == 0:
            _LOG.warning(
                "Replay unavailable: no fresh replay file detected (file is empty)",
            )
            self._notify_obs_instant_replay_unavailable_async("empty_file")
            self._request_launcher_obs_restart_async("empty_file")
            self._snap_idle_for_replay_unavailable("replay_file_empty", log_level=logging.WARNING)
            self._show_replay_unavailable_toast()
            return

        max_age = self._settings.replay_file_max_age_seconds
        assert mtime_epoch is not None and age_sec is not None
        if max_age > 0 and age_sec > max_age:
            _LOG.warning(
                "Replay skipped: file exists but is stale (age_seconds=%s max=%s)",
                int(age_sec),
                max_age,
            )
            self._notify_obs_instant_replay_unavailable_async("stale_file")
            self._request_launcher_obs_restart_async("stale_file")
            self._snap_idle_for_replay_unavailable("replay_file_stale", log_level=logging.WARNING)
            self._show_replay_unavailable_toast()
            return

        if (
            self._last_replayed_file_mtime is not None
            and mtime_epoch == self._last_replayed_file_mtime
        ):
            _LOG.warning("Replay warning: file unchanged since last playback")

        self._replay_pending_mtime = mtime_epoch
        _LOG.info(
            "Replay launch approved: fresh file age_seconds=%s size_bytes=%s",
            int(age_sec),
            size_bytes,
        )

        mpv_executable = self._resolve_mpv_executable()
        if mpv_executable is None:
            _LOG.error("Replay: launch failed — mpv not found")
            self._replay_pending_mtime = None
            self._snap_idle_for_replay_unavailable("mpv_not_found", log_level=logging.ERROR)
            self._show_replay_unavailable_toast()
            return

        input_conf_path = self._ensure_mpv_input_conf()
        if input_conf_path is None:
            _LOG.error("Replay: launch failed — could not write mpv input conf")
            self._replay_pending_mtime = None
            self._snap_idle_for_replay_unavailable("mpv_input_conf_failed", log_level=logging.ERROR)
            self._show_replay_unavailable_toast()
            return

        _LOG.info("Replay: launching mpv executable=%s video=%s", mpv_executable, path)
        self.prepare_canvas_for_video_transition()

        if self._settings.mpv_embedded:
            self.show_video_host()
            self._root.update_idletasks()
            self._embed_spawn_job = self._scheduler.schedule(
                250,
                lambda: self._spawn_mpv_embedded(mpv_executable, input_conf_path),
                name="replay_mpv_embed_delay",
            )
        else:
            self._spawn_mpv_fullscreen(mpv_executable, input_conf_path)

    def _build_mpv_argv(
        self,
        mpv_executable: str,
        input_conf_path: str,
        *,
        embedded_wid: int | None,
    ) -> list[str]:
        """Assemble mpv CLI from Settings (fullscreen and embedded share the same profile)."""
        s = self._settings
        parts: list[str] = [mpv_executable]
        if embedded_wid is not None:
            parts.extend([f"--wid={embedded_wid}", "--no-border"])
        elif s.mpv_fullscreen_enabled:
            # Borderless fills the monitor without exclusive fullscreen / DWM flip modes that
            # often starve OBS display or game capture on Windows.
            if s.mpv_borderless_fullscreen and not s.mpv_embedded:
                # Percent geometry fills the display in mpv's coordinate space (works better than
                # Tk pixel counts on HiDPI / mixed scaling). hidpi-window-scale=no avoids Windows
                # applying an extra scale so the window still covers the full monitor.
                parts.extend(
                    [
                        "--fullscreen=no",
                        "--border=no",
                        "--hidpi-window-scale=no",
                        "--geometry=100%x100%+0+0",
                    ]
                )
            else:
                parts.append("--fs")
        parts.append("--force-window=yes" if s.mpv_force_window_enabled else "--force-window=no")
        parts.append("--keep-open=yes" if s.mpv_keep_open_enabled else "--keep-open=no")
        parts.append("--loop-file=inf" if s.mpv_loop_enabled else "--loop-file=no")
        if s.mpv_obs_friendly:
            # Quality vs GPU load when coexisting with OBS (see MPV_REPLAY_QUALITY).
            q = (s.mpv_replay_quality or "fast").strip().lower()
            if q == "fast":
                parts.append("--profile=fast")
            elif q == "hq":
                parts.append("--profile=gpu-hq")
            else:
                parts.extend(
                    [
                        "--scale=spline36",
                        "--cscale=spline36",
                    ]
                )
        use_sw_only = (
            not s.mpv_hwdec_enabled
            or (s.mpv_obs_friendly and s.mpv_obs_force_software_decode)
        )
        if use_sw_only:
            parts.append("--hwdec=no")
        else:
            mode = (s.mpv_hwdec_mode or "auto").strip() or "auto"
            parts.append(f"--hwdec={mode}")
        vs = (s.mpv_video_sync_mode or "").strip()
        if vs:
            parts.append(f"--video-sync={vs}")
        fd = (s.mpv_framedrop_mode or "").strip()
        if fd:
            parts.append(f"--framedrop={fd}")
        parts.append("--interpolation=yes" if s.mpv_interpolation_enabled else "--interpolation=no")
        parts.append("--audio=no")
        parts.extend(s.mpv_additional_args)
        parts.append("--no-input-terminal")
        parts.append(f"--input-conf={input_conf_path}")
        parts.append(s.replay_video_path)
        return parts

    def _mpv_popen(self, argv: list[str]) -> subprocess.Popen:
        """Windows: optional below-normal priority so OBS keeps scheduling headroom."""
        if os.name != "nt":
            return subprocess.Popen(argv)
        s = self._settings
        below = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", None)
        if below is None:
            return subprocess.Popen(argv)
        want_below = s.mpv_process_priority == "low" or (
            s.mpv_obs_friendly
            and s.mpv_obs_lower_process_priority
            and s.mpv_process_priority == "normal"
        )
        if want_below:
            return subprocess.Popen(argv, creationflags=below)
        return subprocess.Popen(argv)

    def _spawn_mpv_fullscreen(
        self,
        mpv_executable: str,
        input_conf_path: str,
    ) -> None:
        if not self._showing_replay or self._replay_video_active:
            _LOG.info(
                "Replay: mpv fullscreen spawn skipped (invalid state showing=%s active=%s)",
                self._showing_replay,
                self._replay_video_active,
            )
            self._replay_pending_mtime = None
            return

        # Drop root topmost so mpv fullscreen can take the display normally.
        try:
            self._root.attributes("-topmost", False)
            self._root.update_idletasks()
        except tk.TclError:
            _LOG.debug("replay: clear root topmost before mpv failed", exc_info=True)

        mpv_fs_args = self._build_mpv_argv(
            mpv_executable,
            input_conf_path,
            embedded_wid=None,
        )

        try:
            self._replay_video_process = self._mpv_popen(mpv_fs_args)
        except OSError:
            _LOG.exception("Replay: mpv spawn failed (fullscreen); restoring scoreboard")
            self._replay_video_process = None
            self._replay_pending_mtime = None
            self._snap_idle_for_replay_unavailable("mpv_spawn_failed_fullscreen", log_level=logging.ERROR)
            self._show_replay_unavailable_toast()
            return

        self.cancel_slate_stuck_watchdog()
        _LOG.info("Replay: mpv started OK (fullscreen) pid=%s", self._replay_video_process.pid)
        self._commit_replay_file_played()
        self._replay_video_active = True
        self._set_phase(ReplayPhase.VIDEO_PLAYING)
        self._schedule_replay_video_poll()

    def _spawn_mpv_embedded(
        self,
        mpv_executable: str,
        input_conf_path: str,
    ) -> None:
        self._embed_spawn_job = None
        if not self._showing_replay or self._replay_video_active:
            _LOG.info(
                "Replay: mpv embedded spawn skipped (invalid state showing=%s active=%s)",
                self._showing_replay,
                self._replay_video_active,
            )
            self._replay_pending_mtime = None
            self.hide_video_host()
            return

        self._root.update_idletasks()
        host_id = self._video_host.winfo_id()

        mpv_emb_args = self._build_mpv_argv(
            mpv_executable,
            input_conf_path,
            embedded_wid=host_id,
        )

        try:
            self._replay_video_process = self._mpv_popen(mpv_emb_args)
        except OSError:
            _LOG.exception("Replay: mpv spawn failed (embedded); restoring scoreboard")
            self._replay_video_process = None
            self._replay_pending_mtime = None
            self.hide_video_host()
            self._snap_idle_for_replay_unavailable("mpv_spawn_failed_embedded", log_level=logging.ERROR)
            self._show_replay_unavailable_toast()
            return

        self.cancel_slate_stuck_watchdog()
        _LOG.info("Replay: mpv started OK (embedded) pid=%s wid=%s", self._replay_video_process.pid, host_id)
        self._commit_replay_file_played()
        self._replay_video_active = True
        self._set_phase(ReplayPhase.VIDEO_PLAYING)
        self.handoff_replay_to_embedded_video()
        self._schedule_replay_video_poll()

    def prepare_canvas_for_video_transition(self) -> None:
        self._canvas.configure(bg="black")
        self._canvas.itemconfig(self._bg_canvas_id, state="hidden")
        self._canvas.itemconfig("score", state="hidden")

    def restore_canvas_after_video(self) -> None:
        self._canvas.configure(bg="black")
        self._canvas.itemconfig(self._bg_canvas_id, state="normal")
        self._canvas.itemconfig("score", state="normal")
        self._redraw_scores()

    def show_video_host(self) -> None:
        if self._video_host_visible:
            return
        self._video_host.place(x=0, y=0, relwidth=1, relheight=1)
        self._video_host_visible = True
        self._canvas.lift()

    def hide_video_host(self) -> None:
        if not self._video_host_visible:
            return
        self._video_host.place_forget()
        self._video_host_visible = False

    def hide_canvas_for_video_playback(self) -> None:
        self._canvas.pack_forget()

    def show_canvas_after_video(self) -> None:
        self._canvas.pack(fill="both", expand=True)
        self._raise_fullscreen_overlay()
        self.ensure_window_opaque()

    def handoff_replay_to_embedded_video(self) -> None:
        self.ensure_window_opaque()
        self._current_overlay_photo = self._transparent_overlay_photo
        self._canvas.itemconfig(
            self._overlay_canvas_id,
            image=self._current_overlay_photo,
        )
        self.hide_canvas_for_video_playback()

    def ensure_window_opaque(self) -> None:
        try:
            self._root.attributes("-alpha", 1.0)
        except tk.TclError:
            _LOG.debug("Could not set root alpha", exc_info=True)

    def _ensure_mpv_input_conf(self) -> str | None:
        hotkey = (self._settings.mpv_exit_hotkey or "").strip()
        if not hotkey:
            hotkey = "Ctrl+Alt+q"

        conf_line = f"{hotkey} quit\n"

        try:
            fd, conf_path = tempfile.mkstemp(suffix=".conf", prefix="scoreboard_mpv_")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(conf_line)
            self._mpv_input_conf_path = conf_path
            return conf_path
        except OSError:
            _LOG.exception("Replay: failed to write mpv input conf")
            self._mpv_input_conf_path = None
            return None

    def _cleanup_mpv_input_conf(self) -> None:
        if not self._mpv_input_conf_path:
            return
        try:
            if os.path.isfile(self._mpv_input_conf_path):
                os.remove(self._mpv_input_conf_path)
        except OSError:
            _LOG.warning("Replay: could not remove mpv input conf", exc_info=True)
        self._mpv_input_conf_path = None

    def _resolve_mpv_executable(self) -> str | None:
        candidates: list[str] = []
        if self._settings.mpv_path:
            candidates.append(self._settings.mpv_path)
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
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def stop_replay_video_and_return(self) -> None:
        _LOG.info("Replay: operator stopped video (return to slate)")
        self._dismiss_replay_unavailable_toast()
        self.cancel_return_slate()
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()

        if self._showing_replay and not self._is_transitioning:
            self._present_slate_after_mpv_stopped()
            _LOG.info("Replay: scoreboard restored after intentional video stop (slate visible)")

        self.stop_replay_video_process()
        self._replay_video_active = False

        if self._showing_replay and not self._is_transitioning:
            hold_ms = (
                0
                if self._settings.mpv_embedded
                else self._settings.replay_return_slate_hold_ms
            )
            self._schedule_return_to_scoreboard_fade(hold_ms, source="operator_stop")

    def stop_replay_video_process(self) -> None:
        if self._replay_video_process is None:
            return

        process = self._replay_video_process
        self._replay_video_process = None

        if process.poll() is not None:
            _LOG.debug(
                "Replay: mpv already exited pid=%s code=%s",
                process.pid,
                process.returncode,
            )
            return

        try:
            process.terminate()
        except OSError:
            _LOG.warning("Replay: mpv terminate failed", exc_info=True)

        threading.Thread(
            target=self._reap_mpv_process,
            args=(process,),
            daemon=True,
            name="mpv-reap",
        ).start()

    def _reap_mpv_process(self, process: subprocess.Popen) -> None:
        """Block on mpv exit off the Tk thread (terminate/kill/reap)."""
        if process.poll() is not None:
            _LOG.debug(
                "Replay: mpv already exited pid=%s code=%s",
                process.pid,
                process.returncode,
            )
            return
        try:
            process.wait(timeout=1.5)
            _LOG.info("Replay: mpv terminated pid=%s", process.pid)
        except subprocess.TimeoutExpired:
            _LOG.warning("Replay: mpv terminate timed out pid=%s; killing", process.pid)
            try:
                process.kill()
                process.wait(timeout=2.0)
            except OSError:
                _LOG.exception("Replay: mpv kill failed pid=%s", process.pid)
        except OSError:
            _LOG.warning("Replay: mpv wait failed; killing", exc_info=True)
            try:
                process.kill()
            except OSError:
                _LOG.exception("Replay: mpv kill failed")

    def _schedule_replay_video_poll(self) -> None:
        self.cancel_replay_video_poll()
        self._poll_job = self._scheduler.schedule(
            self._settings.replay_video_poll_ms,
            self._poll_replay_video_process,
            name="replay_mpv_poll",
            background_resilience=True,
        )

    def _poll_replay_video_process(self) -> None:
        self._poll_job = None

        process = self._replay_video_process
        if not self._replay_video_active or process is None:
            return

        if process.poll() is None:
            self._schedule_replay_video_poll()
            return

        code = process.returncode
        if code not in (0, None):
            _LOG.warning(
                "Replay: mpv process ended with non-zero code=%s pid=%s (unexpected or user quit)",
                code,
                process.pid,
            )
        else:
            _LOG.info("Replay: mpv process exited pid=%s code=%s", process.pid, code)
        self._replay_video_process = None
        self._replay_video_active = False
        self._dismiss_replay_unavailable_toast()

        if self._showing_replay and not self._is_transitioning:
            self._present_slate_after_mpv_stopped()
            _LOG.info("Replay: scoreboard restored after mpv exit; holding slate before fade-out")
            hold_ms = (
                0
                if self._settings.mpv_embedded
                else self._settings.replay_return_slate_hold_ms
            )
            self._schedule_return_to_scoreboard_fade(hold_ms, source="mpv_exit")
