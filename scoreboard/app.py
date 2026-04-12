"""Tk scoreboard application: orchestration only; features live in sibling modules."""

from __future__ import annotations

import logging
import os
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings, load_settings
from scoreboard.hotkeys import bind_recording_hotkey, bind_recording_hotkey_global
from scoreboard.persistence.score_store import load_scores, save_scores
from scoreboard.platform.win32 import win32_force_foreground, win32_synthetic_click_window_center
from scoreboard.recording_overlay import RecordingOverlay
from scoreboard.replay_controller import ReplayController
from scoreboard.scheduler import AfterScheduler
from scoreboard.screensaver import Screensaver
_LOG = logging.getLogger(__name__)


class ScoreboardApp:
    def __init__(self, root: tk.Tk, settings: Settings | None = None) -> None:
        self.root = root
        self.settings = settings or load_settings()
        self.scheduler = AfterScheduler(
            root,
            logger=_LOG.getChild("scheduler"),
            debug_schedule=self.settings.scoreboard_debug,
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
        self.focus_watchdog_ticks_left = 0
        self._focus_watchdog_exhausted_logged = False
        self.last_input_ms = int(time.monotonic() * 1000)
        self._synthetic_click_attempts = 0

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

        self.recording_overlay = RecordingOverlay(
            root,
            self.settings,
            self.scheduler,
            self.screen_width,
            self.screen_height,
            on_dismiss_chord=self._on_recording_dismiss_chord,
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
        )

        self.draw_scores()

        self.canvas.tag_raise(self.overlay_canvas)

        self._bind_keys()
        self.schedule_idle_check()
        self.schedule_claim_focus()
        self.start_focus_watchdog()
        self.schedule_synthetic_focus_clicks()
        self._schedule_heartbeat()

    def _bind_keys(self) -> None:
        root = self.root
        root.bind(
            "q",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("a", 1)),
        )
        root.bind(
            "a",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("a", -1)),
        )
        root.bind(
            "p",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("b", 1)),
        )
        root.bind(
            "l",
            lambda e: self.on_streamdeck_input(lambda: self.update_score("b", -1)),
        )
        root.bind("r", lambda e: self.on_streamdeck_input(self.reset_scores))
        root.bind("i", lambda e: self.on_streamdeck_input(self.toggle_replay))
        # Chords use bind_all so they still work when the recording Toplevel is focused.
        bind_recording_hotkey_global(
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
        root.bind("<Escape>", lambda e: self.close_app())

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
        self.recording_overlay.lift()

    def on_streamdeck_input(self, action) -> None:
        self.last_input_ms = int(time.monotonic() * 1000)

        if self.screensaver.is_active():
            self.screensaver.stop()
            return

        action()

    def on_recording_start_hotkey(self) -> None:
        if not self.recording_overlay.can_start_countdown_from_hotkey():
            return
        self.recording_overlay.start_or_restart_countdown()

    def _on_recording_dismiss_chord(self, _event: tk.Event | None = None) -> None:
        """Dismiss chord: do not let screensaver-only short-circuit skip dismiss."""
        self.last_input_ms = int(time.monotonic() * 1000)
        if self.screensaver.is_active():
            self.screensaver.stop()
        self.on_recording_dismiss_hotkey()

    def on_recording_dismiss_hotkey(self) -> None:
        if not self.recording_overlay.can_dismiss_ended_from_hotkey():
            return
        self.recording_overlay.dismiss_ended_message()

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
            return
        self.black_screen_active = not self.black_screen_active
        if self.black_screen_active:
            self._show_black_screen_cover()
        else:
            self._hide_black_screen_cover()
        self.recording_overlay.lift()

    def schedule_claim_focus(self) -> None:
        for jid in self._focus_claim_jobs:
            self.scheduler.cancel(jid)
        self._focus_claim_jobs.clear()
        for delay_ms in (0, 50, 150, 400, 800, 1500, 3000, 6000, 12000, 20000):
            jid = self.scheduler.schedule(
                delay_ms,
                self.claim_keyboard_focus,
                name=f"focus_claim_{delay_ms}ms",
            )
            self._focus_claim_jobs.append(jid)

    def claim_keyboard_focus(self) -> None:
        if self.replay.replay_video_active:
            return
        if self.recording_overlay.is_ended_message_showing():
            return

        if self.settings.scoreboard_debug:
            _LOG.debug("Focus reclaim attempt (replay video not active)")

        try:
            self.root.update_idletasks()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.scheduler.cancel(self._release_topmost_job)
            self._release_topmost_job = self.scheduler.schedule(
                150,
                self._release_topmost_brief,
                name="focus_release_topmost",
            )
        except tk.TclError:
            _LOG.debug("claim_keyboard_focus: lift/topmost failed", exc_info=True)

        if os.name == "nt":
            try:
                hwnd = int(self.root.winfo_id())
                _LOG.debug("Focus reclaim: win32_force_foreground hwnd=%s", hwnd)
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

        if not self.replay.replay_video_active:
            self.claim_keyboard_focus()

        self._focus_watchdog_job = self.scheduler.schedule(
            self.settings.focus_watchdog_interval_ms,
            self.focus_watchdog_tick,
            name="focus_watchdog",
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
                name=f"synthetic_click_{delay_ms}ms",
            )
            self._synthetic_click_jobs.append(jid)

    def try_synthetic_focus_click(self) -> None:
        if not self.settings.synthetic_focus_click or self.replay.replay_video_active:
            return
        if self.recording_overlay.is_ended_message_showing():
            return

        if self._synthetic_click_attempts >= 3:
            return

        self._synthetic_click_attempts += 1

        try:
            hwnd = int(self.root.winfo_id())
            if os.name == "nt":
                win32_force_foreground(hwnd)
            win32_synthetic_click_window_center(hwnd)
            _LOG.debug(
                "Synthetic focus click attempt %s hwnd=%s",
                self._synthetic_click_attempts,
                hwnd,
            )
            self.claim_keyboard_focus()
        except (tk.TclError, ValueError, TypeError):
            _LOG.debug("Synthetic focus click failed", exc_info=True)

    def close_app(self) -> None:
        _LOG.info("Application shutdown requested")
        for jid in self._focus_claim_jobs:
            self.scheduler.cancel(jid)
        self._focus_claim_jobs.clear()
        for jid in self._synthetic_click_jobs:
            self.scheduler.cancel(jid)
        self._synthetic_click_jobs.clear()
        self.scheduler.cancel(self._release_topmost_job)
        self._release_topmost_job = None

        self.screensaver.teardown()
        self.replay.teardown()
        self.cancel_focus_watchdog()
        self.scheduler.cancel(self._idle_check_job)
        self._idle_check_job = None
        self.recording_overlay.teardown()
        self.scheduler.cancel(self._heartbeat_job)
        self._heartbeat_job = None
        self.scheduler.cancel_all_tracked()
        self.root.destroy()

    def schedule_idle_check(self) -> None:
        self.scheduler.cancel(self._idle_check_job)
        self._idle_check_job = self.scheduler.schedule(
            5000,
            self.check_idle_timeout,
            name="idle_timeout_check",
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

    def draw_scores(self) -> None:
        self.canvas.delete("score")

        self.score_a_items = self.draw_text_with_effects(
            self.left_x, self.center_y, str(self.score_a)
        )
        self.score_b_items = self.draw_text_with_effects(
            self.right_x, self.center_y, str(self.score_b)
        )

        self.canvas.tag_raise(self.overlay_canvas)
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

        self.score_a = 0
        self.score_b = 0
        self.draw_scores()
        self.save_state()

    def toggle_replay(self) -> None:
        if not self.settings.replay_enabled:
            _LOG.info("Replay hotkey ignored: REPLAY_ENABLED=0")
            return
        self.screensaver.stop()
        self.replay.toggle_replay()

    def ensure_window_opaque(self) -> None:
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            _LOG.debug("ensure_window_opaque failed", exc_info=True)

    def save_state(self) -> None:
        save_scores(self.settings.state_file, self._score_state)
