"""Replay slate fades, mpv launch/embed/fullscreen, polling, recovery, and teardown."""

from __future__ import annotations

import enum
import logging
import os
import shutil
import subprocess
import tempfile
import tkinter as tk
from typing import Callable

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings
from scoreboard.scheduler import AfterScheduler

_LOG = logging.getLogger(__name__)


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

    def restore_normal_scoreboard_state(self, reason: str, *, log_level: int = logging.WARNING) -> None:
        """
        Single known-good path: scores visible, transparent overlay, replay flags cleared.
        Safe to call after launch failure, stuck watchdog, or partial teardown.
        """
        _LOG.log(log_level, "Replay: restoring normal scoreboard (reason=%s)", reason)
        self._cancel_all_replay_timers()
        self.stop_replay_video_process()
        self.hide_video_host()
        try:
            if self._root.winfo_exists():
                self.show_canvas_after_video()
                self.restore_canvas_after_video()
        except tk.TclError:
            _LOG.exception("Replay restore: canvas layout/restore failed (reason=%s)", reason)
        self._current_overlay_photo = self._transparent_overlay_photo
        try:
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._transparent_overlay_photo,
            )
        except tk.TclError:
            _LOG.exception("Replay restore: overlay image failed (reason=%s)", reason)
        self._showing_replay = False
        self._replay_video_active = False
        self._is_transitioning = False
        self._set_phase(ReplayPhase.IDLE)
        self._cleanup_mpv_input_conf()
        try:
            self._after_replay_fade_out()
        except Exception:
            _LOG.exception("Replay restore: after_replay_fade_out failed (reason=%s)", reason)
        _LOG.info("Replay: scoreboard restored to normal mode (reason=%s)", reason)

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
        self.restore_normal_scoreboard_state(
            f"transition_timeout:{phase_label}",
            log_level=logging.ERROR,
        )

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
        self.restore_normal_scoreboard_state("slate_stuck_no_video", log_level=logging.ERROR)

    def teardown(self) -> None:
        _LOG.info("Replay: teardown")
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

    def toggle_replay(self) -> None:
        if not self._settings.replay_enabled:
            _LOG.info("Replay toggle ignored (REPLAY_ENABLED=0)")
            return
        if self._is_transitioning:
            _LOG.debug("Replay toggle ignored during transition")
            return

        _LOG.info(
            "Replay: toggle requested showing=%s video_active=%s phase=%s",
            self._showing_replay,
            self._replay_video_active,
            self._phase.name,
        )

        if self._replay_video_active:
            self.stop_replay_video_and_return()
            return

        if self._showing_replay:
            self.cancel_replay_video_launch()
            self._fade_overlay_out()
        else:
            self._fade_overlay_in()

    def _fade_overlay_in(self) -> None:
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
        _LOG.info("Replay: fade-out from slate starting")
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
        self.hide_video_host()
        self.show_canvas_after_video()
        self.restore_canvas_after_video()
        self._current_overlay_photo = self._transparent_overlay_photo
        self._canvas.itemconfig(
            self._overlay_canvas_id,
            image=self._current_overlay_photo,
        )
        self._showing_replay = False
        self._replay_video_active = False
        self._is_transitioning = False
        self._set_phase(ReplayPhase.IDLE)
        self._cleanup_mpv_input_conf()
        self._after_replay_fade_out()
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
        for i in range(steps + 1):
            alpha = start_alpha + (end_alpha - start_alpha) * (i / steps)
            frame = self._replay_image.copy()
            frame.putalpha(int(alpha))
            photo = ImageTk.PhotoImage(frame)
            self._fade_frames.append(photo)
        self._animate_overlay_fade(0, delay, on_complete)

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
        self._canvas.tag_raise(self._overlay_canvas_id)
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

    def _start_replay_video(self) -> None:
        self._start_job = None

        if not self._showing_replay or self._replay_video_active:
            _LOG.debug(
                "Replay: launch skipped showing=%s active=%s",
                self._showing_replay,
                self._replay_video_active,
            )
            return

        path = self._settings.replay_video_path
        if not path or not os.path.isfile(path):
            _LOG.error("Replay: launch failed — video file missing: %s", path)
            self.restore_normal_scoreboard_state("missing_video_file", log_level=logging.ERROR)
            return

        mpv_executable = self._resolve_mpv_executable()
        if mpv_executable is None:
            _LOG.error("Replay: launch failed — mpv not found")
            self.restore_normal_scoreboard_state("mpv_not_found", log_level=logging.ERROR)
            return

        input_conf_path = self._ensure_mpv_input_conf()
        if input_conf_path is None:
            _LOG.error("Replay: launch failed — could not write mpv input conf")
            self.restore_normal_scoreboard_state("mpv_input_conf_failed", log_level=logging.ERROR)
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

    def _spawn_mpv_fullscreen(
        self,
        mpv_executable: str,
        input_conf_path: str,
    ) -> None:
        if not self._showing_replay or self._replay_video_active:
            return

        try:
            self._replay_video_process = subprocess.Popen(
                [
                    mpv_executable,
                    "--fs",
                    "--force-window=yes",
                    "--keep-open=yes",
                    "--loop-file=inf",
                    "--ontop",
                    "--no-input-terminal",
                    f"--input-conf={input_conf_path}",
                    self._settings.replay_video_path,
                ]
            )
        except OSError:
            _LOG.exception("Replay: mpv spawn failed (fullscreen); restoring scoreboard")
            self._replay_video_process = None
            self.restore_normal_scoreboard_state("mpv_spawn_failed_fullscreen", log_level=logging.ERROR)
            return

        self.cancel_slate_stuck_watchdog()
        _LOG.info("Replay: mpv started OK (fullscreen) pid=%s", self._replay_video_process.pid)
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
            self.hide_video_host()
            return

        self._root.update_idletasks()
        host_id = self._video_host.winfo_id()

        try:
            self._replay_video_process = subprocess.Popen(
                [
                    mpv_executable,
                    f"--wid={host_id}",
                    "--no-border",
                    "--keep-open=yes",
                    "--loop-file=inf",
                    "--no-input-terminal",
                    "--hwdec=no",
                    f"--input-conf={input_conf_path}",
                    self._settings.replay_video_path,
                ]
            )
        except OSError:
            _LOG.exception("Replay: mpv spawn failed (embedded); restoring scoreboard")
            self._replay_video_process = None
            self.hide_video_host()
            self.restore_normal_scoreboard_state("mpv_spawn_failed_embedded", log_level=logging.ERROR)
            return

        self.cancel_slate_stuck_watchdog()
        _LOG.info("Replay: mpv started OK (embedded) pid=%s wid=%s", self._replay_video_process.pid, host_id)
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
        self._canvas.tag_raise(self._overlay_canvas_id)
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
            temp_dir = tempfile.gettempdir()
            conf_path = os.path.join(temp_dir, "scoreboard_mpv_input.conf")
            with open(conf_path, "w", encoding="utf-8") as f:
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
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()

        if self._showing_replay and not self._is_transitioning:
            self.hide_video_host()
            self.show_canvas_after_video()
            self.restore_canvas_after_video()
            self._current_overlay_photo = ImageTk.PhotoImage(self._replay_image)
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._current_overlay_photo,
            )
            self._canvas.tag_raise(self._overlay_canvas_id)
            self._root.update_idletasks()
            self._root.update()
            self._set_phase(ReplayPhase.SLATE_VISIBLE)
            _LOG.info("Replay: scoreboard restored after intentional video stop (slate visible)")

        self.stop_replay_video_process()
        self._replay_video_active = False

        if self._showing_replay and not self._is_transitioning:
            self._return_slate_job = self._scheduler.schedule(
                self._settings.replay_return_slate_hold_ms,
                self._fade_overlay_out,
                name="replay_return_to_slate_hold",
            )

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
            _LOG.warning("Replay: mpv terminate/wait failed; killing", exc_info=True)
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

        if self._showing_replay and not self._is_transitioning:
            self.hide_video_host()
            self.show_canvas_after_video()
            self.restore_canvas_after_video()
            self._current_overlay_photo = ImageTk.PhotoImage(self._replay_image)
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._current_overlay_photo,
            )
            self._canvas.tag_raise(self._overlay_canvas_id)
            self._root.update_idletasks()
            self._root.update()
            self._set_phase(ReplayPhase.SLATE_VISIBLE)
            _LOG.info("Replay: scoreboard restored after mpv exit; holding slate before fade-out")
            self._return_slate_job = self._scheduler.schedule(
                self._settings.replay_return_slate_hold_ms,
                self._fade_overlay_out,
                name="replay_exit_slate_hold",
            )
