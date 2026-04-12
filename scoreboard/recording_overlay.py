"""Recording countdown / max-length message overlay (separate Toplevel)."""

from __future__ import annotations

import enum
import logging
import os
import tkinter as tk
from collections.abc import Callable

from scoreboard.config.settings import Settings
from scoreboard.hotkeys import bind_recording_hotkey
from scoreboard.scheduler import AfterScheduler

_LOG = logging.getLogger(__name__)

# Label-based “on-air” indicator avoids Canvas repaints redrawing sibling Labels on Windows.
_BULB_RECORDING = "\u25cf"  # ●
_BULB_ENDED = "\u25a0"  # ■


class RecordingOverlayState(enum.Enum):
    HIDDEN = enum.auto()
    COUNTING = enum.auto()
    ENDED_MESSAGE = enum.auto()


class RecordingOverlay:
    """Owns the recording Toplevel, countdown, blink, ended message, and timers."""

    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        scheduler: AfterScheduler,
        screen_width: int,
        screen_height: int,
        on_dismiss_chord: Callable[[tk.Event], None],
    ) -> None:
        self._root = root
        self._settings = settings
        self._on_dismiss_chord = on_dismiss_chord
        self._scheduler = scheduler
        self._screen_width = screen_width
        self._screen_height = screen_height

        self._state = RecordingOverlayState.HIDDEN
        self._toplevel: tk.Toplevel | None = None
        self._elapsed_sec = 0
        self._tick_job: str | None = None
        self._blink_job: str | None = None
        self._ended_dismiss_job: str | None = None

        self._bulb_label: tk.Label | None = None
        self._header_label: tk.Label | None = None
        self._main_label: tk.Label | None = None
        self._light_visible = True

    @property
    def state(self) -> RecordingOverlayState:
        return self._state

    def is_ui_active(self) -> bool:
        """True while overlay should block screensaver (counting or ended message)."""
        return self._state in (
            RecordingOverlayState.COUNTING,
            RecordingOverlayState.ENDED_MESSAGE,
        )

    def can_start_countdown_from_hotkey(self) -> bool:
        return self._state != RecordingOverlayState.ENDED_MESSAGE

    def can_dismiss_ended_from_hotkey(self) -> bool:
        return self._state == RecordingOverlayState.ENDED_MESSAGE

    def is_ended_message_showing(self) -> bool:
        return self._state == RecordingOverlayState.ENDED_MESSAGE

    def _geometry(self) -> str:
        w = self._settings.recording_overlay_width
        h = self._settings.recording_overlay_height
        x = self._screen_width - w - 36
        y = 28
        return f"{w}x{h}+{x}+{y}"

    def _ensure_widgets(self) -> None:
        if self._toplevel is not None:
            return

        win = tk.Toplevel(self._root)
        self._toplevel = win
        win.title("")
        win.overrideredirect(True)
        win.configure(bg="black", highlightthickness=0)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            _LOG.debug("Could not set recording overlay topmost", exc_info=True)

        win.geometry(self._geometry())

        outer = tk.Frame(win, bg="black", highlightbackground="#333333", highlightthickness=2)
        outer.pack(fill="both", expand=True, padx=0, pady=0)

        body = tk.Frame(outer, bg="black")
        body.pack(fill="both", expand=True, padx=14, pady=12)

        self._bulb_label = tk.Label(
            body,
            text=_BULB_RECORDING,
            fg="#cc0000",
            bg="black",
            font=("Arial", 28),
            width=1,
            anchor="center",
        )
        self._bulb_label.pack(side="left", padx=(0, 12))

        text_col = tk.Frame(body, bg="black")
        text_col.pack(side="left", fill="both", expand=True)

        wrap = self._settings.recording_overlay_width - 100
        self._header_label = tk.Label(
            text_col,
            text="",
            fg="#cccccc",
            bg="black",
            font=("Arial", 14, "bold"),
            justify="left",
            wraplength=wrap,
            anchor="w",
            takefocus=0,
        )
        self._header_label.pack(side="top", anchor="w")

        self._main_label = tk.Label(
            text_col,
            text="",
            fg="white",
            bg="black",
            font=("Arial", 26, "bold"),
            justify="left",
            wraplength=wrap,
            anchor="w",
            takefocus=0,
        )
        self._main_label.pack(side="top", anchor="w", pady=(6, 0))

        try:
            win.transient(self._root)
        except tk.TclError:
            _LOG.debug("Could not set recording overlay transient", exc_info=True)

        # Dismiss chord on overlay widgets (Windows routes many Alt chords to the focused HWND).
        dismiss_kw = (
            self._settings.recording_dismiss_hotkey,
            "Ctrl+Alt+m",
            self._on_dismiss_chord,
        )
        for w in (win, outer, body, self._bulb_label, self._header_label, self._main_label):
            bind_recording_hotkey(w, *dismiss_kw)

        _LOG.info("Recording overlay widgets created")

    def _set_bulb_recording_shape(self) -> None:
        if self._bulb_label is not None:
            self._bulb_label.configure(text=_BULB_RECORDING)

    def _set_bulb_ended_shape(self) -> None:
        if self._bulb_label is not None:
            self._bulb_label.configure(text=_BULB_ENDED)

    def _cancel_timers(self) -> None:
        self._scheduler.cancel(self._tick_job)
        self._tick_job = None
        self._scheduler.cancel(self._blink_job)
        self._blink_job = None

    def _cancel_ended_dismiss(self) -> None:
        self._scheduler.cancel(self._ended_dismiss_job)
        self._ended_dismiss_job = None

    def _ended_dismiss_fire(self) -> None:
        self._ended_dismiss_job = None
        if self._state != RecordingOverlayState.ENDED_MESSAGE:
            return
        _LOG.info("Recording overlay: auto-dismiss ended message")
        self.dismiss_ended_message()

    def lift(self) -> None:
        if self._toplevel is None:
            return
        try:
            self._toplevel.geometry(self._geometry())
            self._toplevel.lift()
            self._toplevel.attributes("-topmost", True)
        except tk.TclError:
            _LOG.debug("Recording overlay lift failed", exc_info=True)

    def start_or_restart_countdown(self) -> None:
        self._ensure_widgets()
        self._cancel_timers()
        self._cancel_ended_dismiss()

        self._elapsed_sec = 0
        self._state = RecordingOverlayState.COUNTING
        self._light_visible = True
        self._set_bulb_recording_shape()
        self._apply_light_color()

        if self._header_label is not None and self._main_label is not None:
            self._header_label.pack_forget()
            self._main_label.pack_forget()
            self._header_label.configure(
                text=(
                    f"RECORDING: MAX {self._settings.recording_max_minutes} "
                    "MINUTES"
                ),
            )
            self._header_label.pack(side="top", anchor="w")
            self._main_label.pack(side="top", anchor="w", pady=(6, 0))

        if self._main_label is not None:
            self._main_label.configure(
                font=("Arial", 26, "bold"),
                justify="left",
            )
        self._update_countdown_label()

        try:
            self._toplevel.deiconify()
        except tk.TclError:
            _LOG.debug("Recording overlay deiconify failed", exc_info=True)
        self.lift()
        _LOG.info("Recording overlay: countdown started (max %s min)", self._settings.recording_max_minutes)
        self._schedule_tick()
        self._schedule_blink()

    def _schedule_tick(self) -> None:
        self._tick_job = self._scheduler.schedule(
            self._settings.recording_countdown_tick_ms,
            self._countdown_tick,
            name="recording_countdown_tick",
        )

    def _countdown_tick(self) -> None:
        self._tick_job = None
        if self._state != RecordingOverlayState.COUNTING:
            return

        self._elapsed_sec += 1
        if self._elapsed_sec >= self._settings.recording_duration_sec:
            self._elapsed_sec = self._settings.recording_duration_sec
            self._update_countdown_label()
            self._show_max_length_message()
            return

        self._update_countdown_label()
        self._schedule_tick()

    def _update_countdown_label(self) -> None:
        if self._main_label is None:
            return
        total = max(
            0,
            min(self._elapsed_sec, self._settings.recording_duration_sec),
        )
        mm, ss = divmod(total, 60)
        self._main_label.configure(text=f"{mm:02d}:{ss:02d}")

    def _schedule_blink(self) -> None:
        self._blink_job = self._scheduler.schedule(
            self._settings.recording_blink_interval_ms,
            self._blink_tick,
            name="recording_blink",
        )

    def _blink_tick(self) -> None:
        self._blink_job = None
        if not self.is_ui_active():
            return
        if self._state != RecordingOverlayState.COUNTING:
            if self._bulb_label is not None:
                try:
                    self._bulb_label.configure(fg="#cc0000")
                except tk.TclError:
                    _LOG.debug("Recording bulb configure failed", exc_info=True)
            return

        self._light_visible = not self._light_visible
        self._apply_light_color()
        self._schedule_blink()

    def _apply_light_color(self) -> None:
        if self._bulb_label is None:
            return
        fg = "#cc0000" if self._light_visible else "#330000"
        try:
            self._bulb_label.configure(fg=fg)
        except tk.TclError:
            _LOG.debug("Recording bulb apply color failed", exc_info=True)

    def _show_max_length_message(self) -> None:
        self._cancel_timers()
        self._cancel_ended_dismiss()
        self._state = RecordingOverlayState.ENDED_MESSAGE

        if self._header_label is not None:
            self._header_label.pack_forget()

        if self._main_label is not None:
            self._main_label.configure(
                text=self._settings.recording_ended_message,
                font=("Arial", 16, "bold"),
                justify="center",
            )

        self._light_visible = True
        self._set_bulb_ended_shape()
        self._apply_light_color()
        self.lift()
        _LOG.info("Recording overlay: max length reached; showing ended message")
        self._root.after(50, self._grab_focus_for_dismiss_hotkey)

        self._ended_dismiss_job = self._scheduler.schedule(
            self._settings.recording_ended_hold_ms,
            self._ended_dismiss_fire,
            name="recording_ended_auto_dismiss",
        )

    def dismiss_ended_message(self) -> None:
        if self._state != RecordingOverlayState.ENDED_MESSAGE:
            return

        self._cancel_ended_dismiss()
        self._cancel_timers()
        self._state = RecordingOverlayState.HIDDEN

        if self._toplevel is not None:
            try:
                self._toplevel.withdraw()
            except tk.TclError:
                _LOG.debug("Recording overlay withdraw failed", exc_info=True)
        _LOG.info("Recording overlay: ended message dismissed")

    def _grab_focus_for_dismiss_hotkey(self) -> None:
        if self._state != RecordingOverlayState.ENDED_MESSAGE or self._toplevel is None:
            return
        try:
            self._toplevel.lift()
            self._toplevel.attributes("-topmost", True)
            if os.name == "nt":
                from scoreboard.platform.win32 import win32_force_foreground

                hwnd = int(self._toplevel.winfo_id())
                win32_force_foreground(hwnd)
            self._toplevel.focus_force()
        except tk.TclError:
            _LOG.warning("Recording overlay: could not move keyboard focus for dismiss hotkey", exc_info=True)

    def teardown(self) -> None:
        self._cancel_timers()
        self._cancel_ended_dismiss()
        if self._toplevel is not None:
            try:
                self._toplevel.destroy()
            except tk.TclError:
                _LOG.debug("Recording overlay destroy failed", exc_info=True)
            self._toplevel = None
        self._bulb_label = None
        self._header_label = None
        self._main_label = None
        self._state = RecordingOverlayState.HIDDEN

    def on_screen_resize(self, screen_width: int, screen_height: int) -> None:
        self._screen_width = screen_width
        self._screen_height = screen_height
        self.lift()
