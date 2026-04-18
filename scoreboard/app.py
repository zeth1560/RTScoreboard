"""Tk scoreboard application: orchestration only; features live in sibling modules."""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings, load_settings
from scoreboard.hotkeys import bind_recording_hotkey, bind_recording_hotkey_global
from scoreboard.obs_health import check_obs_recording_gate, probe_obs_video_recorder_ready
from scoreboard.encoder_status_overlay import EncoderStatusOverlay
from scoreboard.persistence.score_store import load_scores, save_scores
from scoreboard.platform.win32 import win32_force_foreground, win32_synthetic_click_window_center
from scoreboard.encoder_recording_sync import load_encoder_recording_snapshot
from scoreboard.launcher_status import utc_now_iso, write_launcher_status_json
from scoreboard.recording_overlay import RecordingOverlay, RecordingOverlayState
from scoreboard.replay_buffer_loading_overlay import ReplayBufferLoadingOverlay
from scoreboard.replay_controller import ReplayController
from scoreboard.scheduler import AfterScheduler
from scoreboard.screensaver import Screensaver

_LOG = logging.getLogger(__name__)

# Watchdog focus_ok=False: at most one INFO line per this many seconds (pilot log noise).
_FOCUS_WATCHDOG_FAIL_INFO_COOLDOWN_SEC = 30.0

# Stream Deck: three simultaneous plain-key buttons (q + r + p) → OBS restart (optional).
_OBS_RESTART_CHORD_KEYS = frozenset({"q", "r", "p"})
_OBS_RESTART_CHORD_DEBOUNCE_MS = 75
_OBS_RESTART_COOLDOWN_SEC = 8.0


class ScoreboardApp:
    def __init__(self, root: tk.Tk, settings: Settings | None = None) -> None:
        self.root = root
        self.settings = settings or load_settings()
        self._closing = False
        self.scheduler = AfterScheduler(
            root,
            logger=_LOG.getChild("scheduler"),
            debug_schedule=self.settings.scoreboard_debug,
            alive_check=self._app_is_alive,
        )

        state_path = Path(self.settings.state_file)

        self.root.title("Scoreboard")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")

        self._score_state = load_scores(state_path, rewrite_defaults_if_corrupt=True)

        self._idle_check_job: str | None = None
        self._focus_watchdog_job: str | None = None
        self._focus_claim_jobs: list[str | None] = []
        self._synthetic_click_jobs: list[str | None] = []
        self._release_topmost_job: str | None = None
        self._heartbeat_job: str | None = None
        self._recording_obs_check_in_flight = False
        self._obs_chord_pressed: set[str] = set()
        self._obs_chord_job: str | None = None
        self._obs_restart_last_mono = 0.0
        self._obs_status_win: tk.Toplevel | None = None
        self._obs_status_inner: tk.Frame | None = None
        self._obs_status_label: tk.Label | None = None
        self._obs_status_poll_after: str | None = None
        self._obs_status_poll_busy = False
        self._encoder_recording_poll_job: str | None = None
        self._encoder_recording_prev_seq: int | None = None
        self._encoder_sync_believes_recording = False
        self.focus_watchdog_ticks_left = 0
        self._focus_watchdog_exhausted_logged = False
        self.last_input_ms = int(time.monotonic() * 1000)
        self._synthetic_click_attempts = 0
        self._last_watchdog_focus_fail_info_mono = 0.0

        self.black_screen_active = False
        self.black_screen_cover_visible = False

        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()

        try:
            self.bg_image = (
                Image.open(self.settings.scoreboard_background_image)
                .resize((self.screen_width, self.screen_height))
                .convert("RGBA")
            )
            self.replay_image = (
                Image.open(self.settings.replay_slate_image)
                .resize((self.screen_width, self.screen_height))
                .convert("RGBA")
            )
        except OSError:
            _LOG.exception(
                "Failed to load or decode scoreboard images (paths validated at startup; file may have changed)"
            )
            raise

        self.bg_photo = ImageTk.PhotoImage(self.bg_image)

        self.transparent_overlay = Image.new(
            "RGBA",
            (self.screen_width, self.screen_height),
            (0, 0, 0, 0),
        )
        self.overlay_photo = ImageTk.PhotoImage(self.transparent_overlay)

        self.canvas = tk.Canvas(
            root,
            width=self.screen_width,
            height=self.screen_height,
            highlightthickness=0,
            takefocus=True,
        )
        self.canvas.pack(fill="both", expand=True)
        self.video_host = tk.Frame(root, bg="black")
        self.black_screen_frame = tk.Frame(root, bg="black", highlightthickness=0)
        self.ensure_window_opaque()

        self.bg_canvas = self.canvas.create_image(0, 0, image=self.bg_photo, anchor="nw")
        self.overlay_canvas = self.canvas.create_image(0, 0, image=self.overlay_photo, anchor="nw")

        self.left_x = int(self.screen_width * 0.23)
        self.right_x = int(self.screen_width * 0.77)
        self.center_y = int(self.screen_height * 0.51)
        self.font_size = int(self.screen_height * 0.45)
        self.squeeze_x = 0.88

        self._encoder_status_overlay = EncoderStatusOverlay(
            root,
            self.settings,
            self.scheduler,
            self.canvas,
            self.overlay_canvas,
            self.screen_width,
            self.screen_height,
        )

        self.recording_overlay = RecordingOverlay(
            root,
            self.settings,
            self.scheduler,
            self.screen_width,
            self.screen_height,
            on_dismiss_chord=self._on_recording_dismiss_chord,
            on_ui_visibility=self._encoder_status_overlay.set_recording_overlay_covers,
        )

        self._replay_buffer_loading = ReplayBufferLoadingOverlay(
            root,
            self.settings,
            self.scheduler,
            self.canvas,
            self.overlay_canvas,
            self.screen_width,
            self.screen_height,
        )

        self.screensaver = Screensaver(
            root,
            self.canvas,
            self.overlay_canvas,
            self.settings,
            self.scheduler,
            self.screen_width,
            self.screen_height,
            lift_recording_overlay=self.recording_overlay.lift,
            reclaim_keyboard_focus=lambda: self.claim_keyboard_focus(
                reason="screensaver_periodic",
            ),
            on_stopped=lambda: self.rearm_focus_watchdog_after_transition(
                "screensaver_stopped",
            ),
            after_overlay_raise=self._sync_canvas_aux_overlays,
            on_active_changed=lambda _active: self._publish_launcher_status(),
        )
        self.screensaver.set_transparent_overlay_photo(self.overlay_photo)

        self.replay = ReplayController(
            root,
            self.settings,
            self.scheduler,
            self.canvas,
            self.video_host,
            self.bg_canvas,
            self.overlay_canvas,
            self.replay_image,
            self.overlay_photo,
            lift_recording_overlay=self.recording_overlay.lift,
            before_slate_fade_in=self._hide_black_screen_cover,
            after_replay_fade_out=self._after_replay_fade_out,
            redraw_scores=self.draw_scores,
            after_overlay_raise=self._sync_canvas_aux_overlays,
        )

        self.draw_scores()

        self.canvas.tag_raise(self.overlay_canvas)
        self._sync_canvas_aux_overlays()

        self._encoder_status_overlay.start()

        self._setup_obs_status_indicator()
        self._bind_keys()
        self.schedule_idle_check()
        self.schedule_claim_focus()
        self.start_focus_watchdog()
        self.schedule_synthetic_focus_clicks()
        self._schedule_heartbeat()
        self._apply_hidden_cursor()
        self._schedule_encoder_recording_poll()
        self._publish_launcher_status()
        self._log_startup_readiness()

    def _publish_launcher_status(self) -> None:
        """Emit JSON for ReplayTrove launcher (screensaver + process liveness)."""
        if not self.settings.launcher_status_enabled:
            return
        path = self.settings.launcher_status_json_path
        if not path or not str(path).strip():
            return
        payload = {
            "scoreboard_running": not self._closing,
            "screensaver_active": self.screensaver.is_active(),
            "updated_at": utc_now_iso(),
        }
        write_launcher_status_json(path, payload)

    def _app_is_alive(self) -> bool:
        """False while shutting down — used by AfterScheduler to drop queued work safely.

        Intentionally does **not** use ``winfo_exists()`` on the root: on some Windows/fullscreen
        setups, that call can return 0 intermittently while the UI is healthy. If the scheduler
        skips a callback, recurring jobs like the recording countdown never reschedule and appear
        stuck (or never start, e.g. OBS gate completion never runs).
        """
        return not self._closing

    def _log_startup_readiness(self) -> None:
        log_path = (self.settings.scoreboard_log_file or "").strip()
        _LOG.info(
            "Startup readiness: streamdeck_hotkeys=deferred replay_enabled=%s "
            "recording_overlay=ok scheduler=ok synthetic_focus_click=%s "
            "obs_recording_gate=%s encoder_recording_sync=%s obs_restart_chord=%s "
            "obs_status_indicator=%s log_file=%s",
            self.settings.replay_enabled,
            self.settings.synthetic_focus_click,
            "on"
            if self.settings.recording_obs_health_check
            else "off",
            "on" if self.settings.recording_encoder_sync_enabled else "off",
            "on" if self.settings.obs_restart_chord_enabled else "off",
            "on" if self.settings.obs_status_indicator_enabled else "off",
            repr(log_path) if log_path else "(stderr only)",
        )

    def _schedule_encoder_recording_poll(self) -> None:
        self.scheduler.cancel(self._encoder_recording_poll_job)
        self._encoder_recording_poll_job = None
        if not self.settings.recording_encoder_sync_enabled:
            return
        self._encoder_recording_poll_job = self.scheduler.schedule(
            self.settings.recording_encoder_poll_ms,
            self._encoder_recording_poll_tick,
            name="encoder_recording_poll",
        )

    def _encoder_recording_poll_tick(self) -> None:
        self._encoder_recording_poll_job = None
        if not self.settings.recording_encoder_sync_enabled or self._closing:
            return

        path = Path(self.settings.encoder_state_path)
        snap = load_encoder_recording_snapshot(
            path,
            self.settings.encoder_status_stale_seconds,
            self._encoder_recording_prev_seq,
        )

        if not snap.usable:
            self._schedule_encoder_recording_poll()
            return

        if snap.session_seq is not None:
            self._encoder_recording_prev_seq = snap.session_seq

        capturing = snap.capturing
        was_enc = self._encoder_sync_believes_recording

        if capturing and not was_enc:
            ro = self.recording_overlay
            if ro.state != RecordingOverlayState.COUNTING:
                if ro.can_start_countdown_from_hotkey():
                    _LOG.info(
                        "Recording overlay: countdown started (encoder capture active; %s)",
                        path,
                    )
                    ro.start_or_restart_countdown()
            self._encoder_sync_believes_recording = True
        elif not capturing and was_enc:
            _LOG.info(
                "Recording overlay: encoder idle — hiding in-progress timer if shown (%s)",
                path,
            )
            self.recording_overlay.dismiss_from_encoder_idle()
            self._encoder_sync_believes_recording = False
        else:
            self._encoder_sync_believes_recording = capturing

        self._schedule_encoder_recording_poll()

    def _apply_hidden_cursor(self) -> None:
        """Hide the mouse pointer over the scoreboard (kiosk-style)."""
        cursor = "none"
        try:
            self.root.option_add("*cursor", cursor)
            self.root.configure(cursor=cursor)
        except tk.TclError:
            _LOG.warning(
                "Could not set cursor=%r (invisible pointer may be unsupported); "
                "using system default",
                cursor,
                exc_info=True,
            )
            return
        for w in (self.canvas, self.video_host, self.black_screen_frame):
            try:
                w.configure(cursor=cursor)
            except tk.TclError:
                _LOG.debug("cursor=%r skipped for widget", cursor, exc_info=True)
        if self._obs_status_win is not None:
            try:
                self._obs_status_win.configure(cursor=cursor)
            except tk.TclError:
                _LOG.debug("cursor=%r skipped for obs status", cursor, exc_info=True)
        self.recording_overlay.apply_hidden_cursor()

    def _setup_obs_status_indicator(self) -> None:
        if not self.settings.obs_status_indicator_enabled:
            return

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            _LOG.debug("obs status: topmost unsupported", exc_info=True)
        try:
            win.transient(self.root)
        except tk.TclError:
            _LOG.debug("obs status: transient failed", exc_info=True)
        win.configure(bg="#0d0d0d", highlightthickness=0, cursor="none")

        fz = max(11, min(18, int(self.screen_height * 0.026)))
        inner = tk.Frame(
            win,
            bg="#3d1818",
            highlightthickness=1,
            highlightbackground="#5a2d2d",
        )
        inner.pack(fill="both", expand=True)
        lbl = tk.Label(
            inner,
            text="VIDEO RECORDER UNAVAILABLE",
            font=("Segoe UI", fz, "bold"),
            fg="#ffecec",
            bg="#3d1818",
            padx=16,
            pady=10,
        )
        lbl.pack()

        win.update_idletasks()
        w = max(1, win.winfo_reqwidth())
        h = max(1, win.winfo_reqheight())
        x = 12
        y = max(0, self.screen_height - h - 12)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.deiconify()

        self._obs_status_win = win
        self._obs_status_inner = inner
        self._obs_status_label = lbl

        self._obs_status_poll_after = self.scheduler.schedule(
            300,
            self._obs_status_poll_tick,
            name="obs_status_poll_initial",
        )

    def _apply_obs_status_ready(self, ready: bool) -> None:
        if self._obs_status_label is None or self._obs_status_inner is None:
            return
        if ready:
            bg = "#163a24"
            fg = "#e8ffee"
            hi = "#2d5a3d"
            text = "VIDEO RECORDER READY"
        else:
            bg = "#3d1818"
            fg = "#ffecec"
            hi = "#5a2d2d"
            text = "VIDEO RECORDER UNAVAILABLE"
        self._obs_status_inner.configure(bg=bg, highlightbackground=hi)
        self._obs_status_label.configure(text=text, bg=bg, fg=fg)

    def _obs_status_poll_worker(self) -> None:
        try:
            ready = probe_obs_video_recorder_ready(self.settings)
        except Exception:
            _LOG.debug("OBS status poll failed", exc_info=True)
            ready = False
        self.scheduler.schedule(
            0,
            lambda r=ready: self._obs_status_poll_done(r),
            name="obs_status_poll_done",
        )

    def _obs_status_poll_done(self, ready: bool) -> None:
        self._obs_status_poll_busy = False
        if self._obs_status_win is None:
            return
        self._apply_obs_status_ready(ready)
        self._schedule_obs_status_poll_after()

    def _schedule_obs_status_poll_after(self) -> None:
        if self._obs_status_win is None:
            return
        self.scheduler.cancel(self._obs_status_poll_after)
        self._obs_status_poll_after = None
        ms = self.settings.obs_status_poll_interval_ms
        self._obs_status_poll_after = self.scheduler.schedule(
            ms,
            self._obs_status_poll_tick,
            name="obs_status_poll_tick",
        )

    def _obs_status_poll_tick(self) -> None:
        self._obs_status_poll_after = None
        if self._obs_status_win is None:
            return
        if self._obs_status_poll_busy:
            self._schedule_obs_status_poll_after()
            return
        self._obs_status_poll_busy = True
        threading.Thread(target=self._obs_status_poll_worker, daemon=True).start()

    def _teardown_obs_status_indicator(self) -> None:
        self.scheduler.cancel(self._obs_status_poll_after)
        self._obs_status_poll_after = None
        if self._obs_status_win is not None:
            try:
                self._obs_status_win.destroy()
            except tk.TclError:
                pass
        self._obs_status_win = None
        self._obs_status_inner = None
        self._obs_status_label = None

    def _bind_keys(self) -> None:
        root = self.root
        root.bind_all("q", lambda e, k="q": self._on_obs_restart_chord_key(k, e))
        root.bind_all(
            "a",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("a", -1)),
        )
        root.bind_all("p", lambda e, k="p": self._on_obs_restart_chord_key(k, e))
        root.bind_all(
            "l",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("b", -1)),
        )
        root.bind_all("r", lambda e, k="r": self._on_obs_restart_chord_key(k, e))
        root.bind_all("i", lambda e: self.on_streamdeck_input(self.toggle_replay))
        bind_recording_hotkey(
            root,
            self.settings.recording_start_hotkey,
            "Ctrl+Shift+g",
            lambda e: self.on_streamdeck_input(self.on_recording_start_hotkey),
        )
        bind_recording_hotkey_global(
            root,
            self.settings.recording_dismiss_hotkey,
            "Ctrl+Alt+m",
            self._on_recording_dismiss_chord,
        )
        # Extra binds: main canvas often holds focus; root as fallback.
        bind_recording_hotkey(
            self.canvas,
            self.settings.recording_dismiss_hotkey,
            "Ctrl+Alt+m",
            self._on_recording_dismiss_chord,
        )
        bind_recording_hotkey(
            root,
            self.settings.recording_dismiss_hotkey,
            "Ctrl+Alt+m",
            self._on_recording_dismiss_chord,
        )
        bind_recording_hotkey_global(
            root,
            self.settings.black_screen_hotkey,
            "Ctrl+Shift+b",
            lambda e: self.on_streamdeck_input(self.toggle_black_screen),
        )
        bind_recording_hotkey_global(
            root,
            self.settings.replay_buffer_loading_hotkey,
            "t",
            lambda e: self.on_streamdeck_input(self.start_replay_buffer_loading_overlay),
        )
        root.bind_all(
            "<Escape>",
            lambda e: self.scheduler.schedule(0, self.close_app, name="escape_close_app"),
        )

    def _dispatch_chord_colocated_action(self, key: str) -> None:
        if key == "q":
            self.on_streamdeck_input(lambda: self.update_score("a", 1))
        elif key == "p":
            self.on_streamdeck_input(lambda: self.update_score("b", 1))
        elif key == "r":
            self.on_streamdeck_input(self.reset_scores)

    def _on_obs_restart_chord_key(self, key: str, _event: tk.Event) -> None:
        if not self.settings.obs_restart_chord_enabled:
            self._dispatch_chord_colocated_action(key)
            return
        self._obs_chord_pressed.add(key)
        self.scheduler.cancel(self._obs_chord_job)
        self._obs_chord_job = self.scheduler.schedule(
            _OBS_RESTART_CHORD_DEBOUNCE_MS,
            self._flush_obs_restart_chord,
            name="obs_restart_chord_debounce",
        )

    def _flush_obs_restart_chord(self) -> None:
        self._obs_chord_job = None
        pressed = self._obs_chord_pressed
        self._obs_chord_pressed = set()
        if pressed == _OBS_RESTART_CHORD_KEYS:
            now = time.monotonic()
            if now - self._obs_restart_last_mono < _OBS_RESTART_COOLDOWN_SEC:
                _LOG.debug("OBS restart chord ignored (cooldown)")
                return
            self._obs_restart_last_mono = now
            self._trigger_obs_restart_chord()
            return
        for k in ("q", "p", "r"):
            if k in pressed:
                self._dispatch_chord_colocated_action(k)

    def _trigger_obs_restart_chord(self) -> None:
        self.last_input_ms = int(time.monotonic() * 1000)
        if self.screensaver.is_active():
            self.screensaver.stop()
        _LOG.info("OBS restart chord (Q+R+P): background restart scheduled")
        threading.Thread(target=self._obs_restart_worker, daemon=True).start()

    def _obs_restart_worker(self) -> None:
        from scoreboard.obs_restart import restart_obs_pipeline

        try:
            ok, msg = restart_obs_pipeline(self.settings)
        except Exception:
            _LOG.exception("OBS restart pipeline failed")
            return
        if ok:
            _LOG.info("OBS restart finished: %s", msg)
        else:
            _LOG.warning("OBS restart finished: %s", msg)

    @property
    def score_a(self) -> int:
        return self._score_state.score_a

    @score_a.setter
    def score_a(self, v: int) -> None:
        self._score_state.score_a = v

    @property
    def score_b(self) -> int:
        return self._score_state.score_b

    @score_b.setter
    def score_b(self, v: int) -> None:
        self._score_state.score_b = v

    def _after_replay_fade_out(self) -> None:
        if self.black_screen_active:
            self._show_black_screen_cover()
        # lift() is already invoked from draw_scores → restore_canvas_after_video; calling it
        # again here reapplies Toplevel -topmost and caused a visible flash above the scoreboard.
        self.scheduler.schedule(
            80,
            lambda: self.rearm_focus_watchdog_after_transition("replay_fade_out"),
            name="replay_fade_out_focus_rearm",
        )

    def on_streamdeck_input(self, action: Callable[[], None]) -> None:
        """Bind handlers call this only; work runs on the next event-loop tick."""
        self.scheduler.schedule(
            0,
            lambda a=action: self._run_streamdeck_action(a),
            name="streamdeck_action",
        )

    def _run_streamdeck_action(self, action: Callable[[], None]) -> None:
        self.last_input_ms = int(time.monotonic() * 1000)

        if self.screensaver.is_active():
            self.screensaver.stop()
            return

        action()

    def on_recording_start_hotkey(self) -> None:
        if not self.recording_overlay.can_start_countdown_from_hotkey():
            return
        if self.settings.recording_obs_health_check:
            if self._recording_obs_check_in_flight:
                _LOG.debug("Recording OBS check already running; ignoring duplicate start hotkey")
                return
            self._recording_obs_check_in_flight = True
            threading.Thread(
                target=self._recording_start_obs_check_worker,
                daemon=True,
            ).start()
        else:
            self.recording_overlay.start_or_restart_countdown()

    def _recording_start_obs_check_worker(self) -> None:
        try:
            ok, msg = check_obs_recording_gate(self.settings)
        except Exception:
            _LOG.exception("OBS recording gate failed unexpectedly")
            ok, msg = False, "Could not verify OBS (unexpected error); see logs."
        self.scheduler.schedule(
            0,
            lambda o=ok, m=msg: self._on_recording_obs_check_done(o, m),
            name="recording_obs_gate_done",
        )

    def _on_recording_obs_check_done(self, ok: bool, msg: str) -> None:
        self._recording_obs_check_in_flight = False
        if ok:
            if not self.recording_overlay.can_start_countdown_from_hotkey():
                return
            self.recording_overlay.start_or_restart_countdown()
            self._apply_obs_status_ready(True)
            return
        _LOG.warning("Recording overlay not started: %s", msg)
        self._apply_obs_status_ready(False)
        if not self.settings.recording_obs_health_fail_closed:
            if not self.recording_overlay.can_start_countdown_from_hotkey():
                return
            _LOG.warning("OBS gate failed; fail-open enabled, starting timer anyway")
            self.recording_overlay.start_or_restart_countdown()
            return
        self.replay.show_replay_unavailable_graphic_overlay()

    def _on_recording_dismiss_chord(self, _event: tk.Event | None = None) -> None:
        """Dismiss chord: do not let screensaver-only short-circuit skip dismiss."""
        self.scheduler.schedule(
            0,
            self._recording_dismiss_deferred,
            name="recording_dismiss_deferred",
        )

    def _recording_dismiss_deferred(self) -> None:
        self.last_input_ms = int(time.monotonic() * 1000)
        if self.screensaver.is_active():
            self.screensaver.stop()
        self.on_recording_dismiss_hotkey()

    def on_recording_dismiss_hotkey(self) -> None:
        if not self.recording_overlay.can_dismiss_from_operator_hotkey():
            return
        self.recording_overlay.dismiss_from_operator_hotkey()

    def _show_black_screen_cover(self) -> None:
        if self.black_screen_cover_visible:
            return
        self.black_screen_frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.black_screen_frame.lift()
        self.black_screen_cover_visible = True
        self.recording_overlay.lift()

    def _hide_black_screen_cover(self) -> None:
        if not self.black_screen_cover_visible:
            return
        self.black_screen_frame.place_forget()
        self.black_screen_cover_visible = False

    def toggle_black_screen(self) -> None:
        if self.replay.blocks_black_screen_toggle():
            _LOG.info(
                "Black screen toggle ignored (replay busy transitioning=%s video=%s showing=%s)",
                self.replay.is_transitioning,
                self.replay.replay_video_active,
                self.replay.showing_replay,
            )
            return
        self.black_screen_active = not self.black_screen_active
        if self.black_screen_active:
            self._show_black_screen_cover()
        else:
            self._hide_black_screen_cover()
            self.rearm_focus_watchdog_after_transition("black_screen_off")
        self.recording_overlay.lift()

    def schedule_claim_focus(self) -> None:
        for jid in self._focus_claim_jobs:
            self.scheduler.cancel(jid)
        self._focus_claim_jobs.clear()
        for delay_ms in (0, 50, 150, 400, 800, 1500, 3000, 6000, 12000, 20000):
            jid = self.scheduler.schedule(
                delay_ms,
                lambda ms=delay_ms: self.claim_keyboard_focus(
                    reason=f"startup_claim_{ms}ms",
                ),
                name=f"focus_claim_{delay_ms}ms",
            )
            self._focus_claim_jobs.append(jid)

    def rearm_focus_watchdog_after_transition(self, event: str) -> None:
        """Extend pilot protection: full watchdog duration + startup-style claim burst."""
        approx_s = (
            self.settings.focus_watchdog_ticks
            * self.settings.focus_watchdog_interval_ms
            // 1000
        )
        _LOG.info(
            "Focus: re-arming watchdog after %s (~%s s of periodic reclaim, interval %sms)",
            event,
            approx_s,
            self.settings.focus_watchdog_interval_ms,
        )
        self.start_focus_watchdog()
        self.schedule_claim_focus()

    def _focus_keyboard_seems_on_app(self) -> bool:
        w = self.root.focus_get()
        if w is None:
            return False
        try:
            top = w.winfo_toplevel()
        except tk.TclError:
            return False
        if top == self.root:
            return True
        rec = self.recording_overlay.recording_toplevel()
        return rec is not None and top == rec

    def _focus_reclaim_eligible(self) -> bool:
        """Whether periodic / automatic reclaim should run (centralized guard)."""
        if not self._app_is_alive():
            return False
        if self.replay.replay_video_active:
            return False
        if self.black_screen_active:
            return False
        if self.replay.blocks_idle():
            return False
        if self.recording_overlay.is_ended_message_showing():
            return False
        return True

    def claim_keyboard_focus(self, *, reason: str = "unspecified") -> None:
        if not self._focus_reclaim_eligible():
            _LOG.debug("Focus reclaim skipped (reason=%s): ineligible context", reason)
            return

        used_win32 = os.name == "nt" and not self.recording_overlay.is_ui_active()

        try:
            self.root.update_idletasks()
            self.root.lift()
            # Root topmost on/off makes a transient recording Toplevel flicker on Windows.
            # While the recording box is up, keep the root out of that dance; overlay stays topmost.
            if not self.recording_overlay.is_ui_active():
                self.root.attributes("-topmost", True)
                self.scheduler.cancel(self._release_topmost_job)
                self._release_topmost_job = self.scheduler.schedule(
                    150,
                    self._release_topmost_brief,
                    name="focus_release_topmost",
                )
        except tk.TclError:
            _LOG.debug("claim_keyboard_focus: lift/topmost failed", exc_info=True)

        if used_win32:
            try:
                hwnd = int(self.root.winfo_id())
                win32_force_foreground(hwnd)
            except (tk.TclError, ValueError, TypeError):
                _LOG.debug("Focus reclaim: win32_force_foreground skipped", exc_info=True)

        try:
            self.root.focus_force()
            self.root.focus_set()
            self.canvas.focus_set()
            self.canvas.focus_force()
        except tk.TclError:
            _LOG.debug("claim_keyboard_focus: focus_set failed", exc_info=True)

        focus_ok = self._focus_keyboard_seems_on_app()
        if reason.startswith("after_synthetic_click"):
            lvl = logging.INFO
        elif reason == "watchdog":
            if not focus_ok:
                now_mono = time.monotonic()
                if (
                    now_mono - self._last_watchdog_focus_fail_info_mono
                    >= _FOCUS_WATCHDOG_FAIL_INFO_COOLDOWN_SEC
                ):
                    self._last_watchdog_focus_fail_info_mono = now_mono
                    lvl = logging.INFO
                else:
                    lvl = logging.DEBUG
            else:
                lvl = logging.DEBUG
        else:
            lvl = logging.DEBUG
        _LOG.log(
            lvl,
            "Focus reclaim: reason=%s win32_foreground=%s focus_ok=%s focus_widget=%r",
            reason,
            used_win32,
            focus_ok,
            self.root.focus_get(),
        )

    def _release_topmost_brief(self) -> None:
        self._release_topmost_job = None
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            _LOG.debug("release topmost failed", exc_info=True)

    def start_focus_watchdog(self) -> None:
        self.cancel_focus_watchdog()
        self.focus_watchdog_ticks_left = self.settings.focus_watchdog_ticks
        self._focus_watchdog_job = self.scheduler.schedule(
            self.settings.focus_watchdog_interval_ms,
            self.focus_watchdog_tick,
            name="focus_watchdog",
            background_resilience=True,
        )
        self._focus_watchdog_exhausted_logged = False

    def cancel_focus_watchdog(self) -> None:
        self.scheduler.cancel(self._focus_watchdog_job)
        self._focus_watchdog_job = None

    def focus_watchdog_tick(self) -> None:
        self._focus_watchdog_job = None

        if self.focus_watchdog_ticks_left <= 0:
            if not self._focus_watchdog_exhausted_logged:
                self._focus_watchdog_exhausted_logged = True
                _LOG.info(
                    "Focus watchdog: initial reclaim phase finished after %s ticks "
                    "(no further periodic focus reclaim; manual input / restarts still apply)",
                    self.settings.focus_watchdog_ticks,
                )
            return

        self.focus_watchdog_ticks_left -= 1

        if self._focus_reclaim_eligible():
            self.claim_keyboard_focus(reason="watchdog")

        self._focus_watchdog_job = self.scheduler.schedule(
            self.settings.focus_watchdog_interval_ms,
            self.focus_watchdog_tick,
            name="focus_watchdog",
            background_resilience=True,
        )

    def schedule_synthetic_focus_clicks(self) -> None:
        if not self.settings.synthetic_focus_click:
            return
        for jid in self._synthetic_click_jobs:
            self.scheduler.cancel(jid)
        self._synthetic_click_jobs.clear()
        for delay_ms in (2500, 6000, 12000):
            jid = self.scheduler.schedule(
                delay_ms,
                self.try_synthetic_focus_click,
                name="synthetic_focus_click",
                background_resilience=True,
            )
            self._synthetic_click_jobs.append(jid)

    def try_synthetic_focus_click(self) -> None:
        if not self.settings.synthetic_focus_click:
            return
        if not self._focus_reclaim_eligible():
            return

        if self._synthetic_click_attempts >= 3:
            return

        self._synthetic_click_attempts += 1

        try:
            hwnd = int(self.root.winfo_id())
            if os.name == "nt":
                win32_force_foreground(hwnd)
            win32_synthetic_click_window_center(hwnd)
            _LOG.info(
                "Focus: synthetic click attempt %s/3 (hwnd=%s); follow-up reclaim",
                self._synthetic_click_attempts,
                hwnd,
            )
            self.claim_keyboard_focus(
                reason=f"after_synthetic_click_{self._synthetic_click_attempts}",
            )
        except (tk.TclError, ValueError, TypeError):
            _LOG.debug("Synthetic focus click failed", exc_info=True)

    def close_app(self) -> None:
        if self._closing:
            return
        self._closing = True
        _LOG.info("Application shutdown requested")
        self._obs_status_poll_busy = False
        self._recording_obs_check_in_flight = False
        self.scheduler.cancel(self._obs_chord_job)
        self._obs_chord_job = None
        self._teardown_obs_status_indicator()
        for jid in self._focus_claim_jobs:
            self.scheduler.cancel(jid)
        self._focus_claim_jobs.clear()
        for jid in self._synthetic_click_jobs:
            self.scheduler.cancel(jid)
        self._synthetic_click_jobs.clear()
        self.scheduler.cancel(self._release_topmost_job)
        self._release_topmost_job = None
        self.scheduler.cancel(self._encoder_recording_poll_job)
        self._encoder_recording_poll_job = None

        self.screensaver.teardown()
        self._encoder_status_overlay.teardown()
        self._replay_buffer_loading.teardown()
        self.replay.teardown()
        self.cancel_focus_watchdog()
        self.scheduler.cancel(self._idle_check_job)
        self._idle_check_job = None
        self.recording_overlay.teardown()
        self.scheduler.cancel(self._heartbeat_job)
        self._heartbeat_job = None
        self.scheduler.cancel_all_tracked()
        self._publish_launcher_status()
        self.root.destroy()

    def schedule_idle_check(self) -> None:
        self.scheduler.cancel(self._idle_check_job)
        self._idle_check_job = self.scheduler.schedule(
            5000,
            self.check_idle_timeout,
            name="idle_timeout_check",
            background_resilience=True,
        )

    def check_idle_timeout(self) -> None:
        self._idle_check_job = None

        now_ms = int(time.monotonic() * 1000)
        idle_ms = now_ms - self.last_input_ms

        if (
            self.settings.slideshow_enabled
            and not self.screensaver.is_active()
            and not self.replay.blocks_idle()
            and not self.recording_overlay.is_ui_active()
            and not self.black_screen_active
            and idle_ms >= self.settings.idle_timeout_ms
        ):
            self.screensaver.start()

        self.schedule_idle_check()

    def _schedule_heartbeat(self) -> None:
        self.scheduler.cancel(self._heartbeat_job)
        self._heartbeat_job = None
        n = self.settings.heartbeat_interval_minutes
        if n <= 0:
            return
        ms = n * 60 * 1000
        self._heartbeat_job = self.scheduler.schedule(
            ms,
            self._heartbeat_tick,
            name="pilot_heartbeat",
            background_resilience=True,
        )

    def _heartbeat_tick(self) -> None:
        self._heartbeat_job = None
        try:
            _LOG.info(
                "heartbeat alive replay_phase=%s replay_video=%s screensaver=%s "
                "recording_ui=%s black_screen=%s",
                self.replay.phase.name,
                self.replay.replay_video_active,
                self.screensaver.is_active(),
                self.recording_overlay.is_ui_active(),
                self.black_screen_active,
            )
        except Exception:
            _LOG.exception("heartbeat logging failed")
        self._schedule_heartbeat()

    def create_scaled_text(self, x: int, y: int, text: str, color: str):
        item = self.canvas.create_text(
            x,
            y,
            text=text,
            fill=color,
            font=("Arial", self.font_size, "bold"),
            tags="score",
        )
        self.canvas.scale(item, x, y, self.squeeze_x, 1.0)
        return item

    def draw_text_with_effects(self, x: int, y: int, text: str):
        items = []

        shadow_offset = int(self.font_size * 0.03)
        outline_offset = int(self.font_size * 0.015)

        items.append(
            self.create_scaled_text(
                x + shadow_offset,
                y + shadow_offset,
                text,
                "#000000",
            )
        )

        for dx in [-outline_offset, 0, outline_offset]:
            for dy in [-outline_offset, 0, outline_offset]:
                if dx == 0 and dy == 0:
                    continue
                items.append(
                    self.create_scaled_text(
                        x + dx,
                        y + dy,
                        text,
                        "#000000",
                    )
                )

        items.append(
            self.create_scaled_text(
                x,
                y,
                text,
                "#FFFFFF",
            )
        )

        return items

    def _sync_canvas_aux_overlays(self) -> None:
        """Keep encoder + replay-buffer canvas strips above the transparent overlay."""
        self._encoder_status_overlay.sync_canvas_stack()
        self._replay_buffer_loading.sync_canvas_stack()

    def draw_scores(self) -> None:
        self.canvas.delete("score")

        self.score_a_items = self.draw_text_with_effects(
            self.left_x, self.center_y, str(self.score_a)
        )
        self.score_b_items = self.draw_text_with_effects(
            self.right_x, self.center_y, str(self.score_b)
        )

        self.canvas.tag_raise(self.overlay_canvas)
        self._sync_canvas_aux_overlays()
        self.recording_overlay.lift()

    def update_score(self, team: str, delta: int) -> None:
        if self.black_screen_active:
            return
        if self.replay.blocks_score_updates():
            return

        if team == "a":
            self.score_a = max(0, min(99, self.score_a + delta))
        else:
            self.score_b = max(0, min(99, self.score_b + delta))

        self.draw_scores()
        self.save_state()

    def reset_scores(self) -> None:
        if self.replay.blocks_score_updates():
            return

        if self.black_screen_active:
            self.black_screen_active = False
            self._hide_black_screen_cover()
            self.recording_overlay.lift()
            self.rearm_focus_watchdog_after_transition("black_screen_off_reset")

        self.score_a = 0
        self.score_b = 0
        self.draw_scores()
        self.save_state()

    def toggle_replay(self) -> None:
        if self.replay.dismiss_replay_unavailable_overlay():
            return
        if not self.settings.replay_enabled:
            _LOG.info("Replay hotkey ignored: REPLAY_ENABLED=0")
            return
        self.screensaver.stop()
        self.replay.toggle_replay()

    def start_replay_buffer_loading_overlay(self) -> None:
        self._replay_buffer_loading.start_sequence()

    def ensure_window_opaque(self) -> None:
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            _LOG.debug("ensure_window_opaque failed", exc_info=True)

    def save_state(self) -> None:
        save_scores(self.settings.state_file, self._score_state)
