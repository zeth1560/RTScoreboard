"""Recording countdown / max-length overlay (Toplevel): graphic slates or legacy labels."""

from __future__ import annotations

import enum
import logging
import os
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings
from scoreboard.scheduler import AfterScheduler

_LOG = logging.getLogger(__name__)


class RecordingOverlayState(enum.Enum):
    HIDDEN = enum.auto()
    COUNTING = enum.auto()
    ENDED_MESSAGE = enum.auto()
    SESSION_END_INFO = enum.auto()


class RecordingOverlay:
    """Recording UI: optional PNG slates with overlaid timer, or legacy text layout."""

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
        self._session_end_info_job: str | None = None
        self._last_applied_geometry: str | None = None

        self._outer: tk.Frame | None = None
        self._canvas: tk.Canvas | None = None
        self._bg_image_id: int | None = None
        self._timer_text_id: int | None = None
        self._tk_photo_progress_on: ImageTk.PhotoImage | None = None
        self._tk_photo_progress_off: ImageTk.PhotoImage | None = None
        self._tk_photo_ended: ImageTk.PhotoImage | None = None
        self._graphic_cache_wh: tuple[int, int] | None = None
        self._light_visible = True

        self._body_inner: tk.Frame | None = None
        self._text_col: tk.Frame | None = None
        self._light_canvas: tk.Canvas | None = None
        self._light_shape_id: int | None = None
        self._header_label: tk.Label | None = None
        self._main_label: tk.Label | None = None

    @property
    def state(self) -> RecordingOverlayState:
        return self._state

    def recording_toplevel(self) -> tk.Toplevel | None:
        """Main recording popup, if created; used for focus-belongs-to-app checks."""
        return self._toplevel

    def is_ui_active(self) -> bool:
        return self._state in (
            RecordingOverlayState.COUNTING,
            RecordingOverlayState.ENDED_MESSAGE,
            RecordingOverlayState.SESSION_END_INFO,
        )

    def is_ended_message_showing(self) -> bool:
        """Slates after max length / session end; avoid synthetic clicks that steal dismiss focus."""
        return self._state in (
            RecordingOverlayState.ENDED_MESSAGE,
            RecordingOverlayState.SESSION_END_INFO,
        )

    def can_start_countdown_from_hotkey(self) -> bool:
        return self._state not in (
            RecordingOverlayState.ENDED_MESSAGE,
            RecordingOverlayState.SESSION_END_INFO,
        )

    def can_dismiss_from_operator_hotkey(self) -> bool:
        return self._state in (
            RecordingOverlayState.COUNTING,
            RecordingOverlayState.ENDED_MESSAGE,
            RecordingOverlayState.SESSION_END_INFO,
        )

    def _geometry(self) -> str:
        w = self._settings.recording_overlay_width
        h = self._settings.recording_overlay_height
        x = self._screen_width - w - 36
        y = 28
        return f"{w}x{h}+{x}+{y}"

    def _canvas_progress_enabled(self) -> bool:
        p = self._settings.recording_progress_image_on
        return bool(p and os.path.isfile(p))

    def _canvas_ended_enabled(self) -> bool:
        p = self._settings.recording_ended_image
        return bool(p and os.path.isfile(p))

    def _canvas_blink_pair_enabled(self) -> bool:
        if not self._canvas_progress_enabled():
            return False
        on = self._settings.recording_progress_image_on
        off = self._settings.recording_progress_image_off
        if not off or not os.path.isfile(off):
            return False
        return os.path.normcase(os.path.normpath(on)) != os.path.normcase(
            os.path.normpath(off)
        )

    def _timer_font_tuple(self) -> tuple[str, int, str]:
        size = self._settings.recording_overlay_timer_font_size
        for name in ("Impact", "Arial Narrow", "Arial"):
            if name in tkfont.families(self._root):
                return (name, size, "bold")
        return ("Arial", size, "bold")

    def _timer_canvas_xy(self) -> tuple[int, int]:
        w = self._settings.recording_overlay_width
        h = self._settings.recording_overlay_height
        tx = int(w * self._settings.recording_overlay_timer_x_frac)
        ty = int(h * self._settings.recording_overlay_timer_y_frac)
        tx += self._settings.recording_overlay_timer_offset_x_px
        ty += self._settings.recording_overlay_timer_offset_y_px
        return max(0, tx), max(0, ty)

    def _load_resized_photo(self, path: str) -> ImageTk.PhotoImage | None:
        try:
            with Image.open(path) as img:
                rgba = img.convert("RGBA")
                tw, th = (
                    self._settings.recording_overlay_width,
                    self._settings.recording_overlay_height,
                )
                if rgba.size != (tw, th):
                    rgba = rgba.resize((tw, th), Image.Resampling.LANCZOS)
                return ImageTk.PhotoImage(rgba)
        except OSError:
            _LOG.exception("Recording overlay: could not load image %s", path)
            return None

    def _invalidate_graphic_photos(self) -> None:
        self._tk_photo_progress_on = None
        self._tk_photo_progress_off = None
        self._tk_photo_ended = None
        self._graphic_cache_wh = None
        if self._canvas is not None:
            try:
                self._canvas.delete("all")
            except tk.TclError:
                pass
        self._bg_image_id = None
        self._timer_text_id = None

    def _ensure_graphic_photos_loaded(self) -> bool:
        wh = (
            self._settings.recording_overlay_width,
            self._settings.recording_overlay_height,
        )
        if self._graphic_cache_wh == wh and self._tk_photo_progress_on is not None:
            return True
        self._invalidate_graphic_photos()
        on_path = self._settings.recording_progress_image_on
        po = self._load_resized_photo(on_path) if on_path else None
        if po is None:
            return False
        self._tk_photo_progress_on = po
        self._graphic_cache_wh = wh
        off_path = self._settings.recording_progress_image_off
        if off_path and os.path.isfile(off_path):
            self._tk_photo_progress_off = self._load_resized_photo(off_path)
        end_path = self._settings.recording_ended_image
        if end_path and os.path.isfile(end_path):
            self._tk_photo_ended = self._load_resized_photo(end_path)
        return True

    def _ensure_ended_photo_loaded(self) -> bool:
        wh = (
            self._settings.recording_overlay_width,
            self._settings.recording_overlay_height,
        )
        if self._tk_photo_ended is not None and self._graphic_cache_wh == wh:
            return True
        path = self._settings.recording_ended_image
        if not path or not os.path.isfile(path):
            return False
        pe = self._load_resized_photo(path)
        if pe is None:
            return False
        self._tk_photo_ended = pe
        self._graphic_cache_wh = wh
        return True

    def _ensure_widgets(self) -> None:
        if self._toplevel is not None and self._light_canvas is not None:
            return
        if self._toplevel is not None:
            _LOG.warning(
                "Recording overlay: discarding incomplete Toplevel and rebuilding widgets"
            )
            try:
                self._toplevel.destroy()
            except tk.TclError:
                pass
            self._toplevel = None
            self._outer = None
            self._canvas = None
            self._body_inner = None
            self._text_col = None
            self._header_label = None
            self._main_label = None
            self._bg_image_id = None
            self._timer_text_id = None
            self._light_canvas = None
            self._light_shape_id = None
            self._invalidate_graphic_photos()

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

        outer = tk.Frame(win, bg="black", highlightthickness=0)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        w, h = self._settings.recording_overlay_width, self._settings.recording_overlay_height
        self._canvas = tk.Canvas(
            outer,
            width=w,
            height=h,
            bg="black",
            highlightthickness=0,
        )

        body = tk.Frame(outer, bg="black")
        self._body_inner = body

        light_canvas = tk.Canvas(
            body,
            width=48,
            height=48,
            bg="black",
            highlightthickness=0,
            highlightbackground="black",
        )
        self._light_canvas = light_canvas
        self._rebuild_circle()
        light_canvas.pack(side="left", padx=(0, 12))

        text_col = tk.Frame(body, bg="black")
        text_col.pack(side="left", fill="both", expand=True)
        self._text_col = text_col

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

        self.apply_hidden_cursor()
        _LOG.info("Recording overlay widgets created")

    def apply_hidden_cursor(self) -> None:
        """Match main scoreboard: invisible pointer over the recording box."""
        if self._toplevel is None:
            return
        cursor = "none"
        for w in (
            self._toplevel,
            self._outer,
            self._canvas,
            self._body_inner,
            self._light_canvas,
            self._text_col,
            self._header_label,
            self._main_label,
        ):
            if w is not None:
                try:
                    w.configure(cursor=cursor)
                except tk.TclError:
                    _LOG.debug("recording overlay cursor=%r skipped", cursor, exc_info=True)

    def _rebuild_circle(self) -> None:
        c = self._light_canvas
        if c is None:
            return
        c.delete("all")
        self._light_shape_id = c.create_oval(
            4, 4, 40, 40, fill="#cc0000", outline="#660000", width=2
        )

    def _rebuild_square(self) -> None:
        c = self._light_canvas
        if c is None:
            return
        c.delete("all")
        self._light_shape_id = c.create_rectangle(
            4, 4, 40, 40, fill="#cc0000", outline="#660000", width=2
        )

    def _show_canvas_layer(self) -> None:
        if self._canvas is None or self._body_inner is None:
            return
        try:
            self._body_inner.pack_forget()
        except tk.TclError:
            pass
        self._canvas.pack(fill="both", expand=True)

    def _show_legacy_layer(self) -> None:
        if self._canvas is None or self._body_inner is None:
            return
        try:
            self._canvas.pack_forget()
        except tk.TclError:
            pass
        self._body_inner.pack(fill="both", expand=True, padx=14, pady=12)

    def _ensure_light_packed(self) -> None:
        if self._light_canvas is None or self._text_col is None:
            return
        try:
            self._light_canvas.pack_info()
        except tk.TclError:
            self._light_canvas.pack(side="left", padx=(0, 12), before=self._text_col)

    def _build_canvas_items(self, base_photo: ImageTk.PhotoImage) -> None:
        if self._canvas is None:
            return
        self._canvas.delete("all")
        self._bg_image_id = self._canvas.create_image(
            0, 0, anchor="nw", image=base_photo, tags=("bg",)
        )
        tx, ty = self._timer_canvas_xy()
        self._timer_text_id = self._canvas.create_text(
            tx,
            ty,
            text="00:00",
            fill="#ffffff",
            font=self._timer_font_tuple(),
            anchor="center",
            tags=("timer",),
        )

    def _set_canvas_background_photo(self, photo: ImageTk.PhotoImage) -> None:
        if self._canvas is None or self._bg_image_id is None:
            return
        try:
            self._canvas.itemconfig(self._bg_image_id, image=photo)
        except tk.TclError:
            _LOG.debug("canvas itemconfig bg failed", exc_info=True)

    def _cancel_timers(self) -> None:
        self._scheduler.cancel(self._tick_job)
        self._tick_job = None
        self._scheduler.cancel(self._blink_job)
        self._blink_job = None

    def _cancel_ended_dismiss(self) -> None:
        self._scheduler.cancel(self._ended_dismiss_job)
        self._ended_dismiss_job = None

    def _cancel_session_end_info(self) -> None:
        self._scheduler.cancel(self._session_end_info_job)
        self._session_end_info_job = None

    def _ended_dismiss_fire(self) -> None:
        self._ended_dismiss_job = None
        if self._state != RecordingOverlayState.ENDED_MESSAGE:
            return
        _LOG.info("Recording overlay: auto-dismiss max-length message")
        self._hide_completely()

    def _session_end_info_fire(self) -> None:
        self._session_end_info_job = None
        if self._state != RecordingOverlayState.SESSION_END_INFO:
            return
        self._hide_completely()

    def _hide_completely(self) -> None:
        self._cancel_ended_dismiss()
        self._cancel_session_end_info()
        self._cancel_timers()
        self._state = RecordingOverlayState.HIDDEN
        self._light_visible = True

        if self._toplevel is not None:
            try:
                self._toplevel.withdraw()
            except tk.TclError:
                _LOG.debug("Recording overlay withdraw failed", exc_info=True)
        self._last_applied_geometry = None
        _LOG.info("Recording overlay: hidden")

    def lift(self) -> None:
        """Keep overlay stacked above content when visible. No-op when dismissed (withdrawn)."""
        if self._toplevel is None:
            return
        if self._state == RecordingOverlayState.HIDDEN:
            # Critical: lift()/wm raise on a withdrawn Toplevel remaps the window on Windows,
            # so any draw_scores/replay/screensaver tick would resurrect the "dismissed" box.
            return
        try:
            geom = self._geometry()
            if geom != self._last_applied_geometry:
                self._toplevel.geometry(geom)
                self._last_applied_geometry = geom
            self._toplevel.lift()
            self._toplevel.attributes("-topmost", True)
        except tk.TclError:
            _LOG.debug("Recording overlay lift failed", exc_info=True)

    def start_or_restart_countdown(self) -> None:
        self._ensure_widgets()
        self._cancel_timers()
        self._cancel_ended_dismiss()
        self._cancel_session_end_info()

        self._elapsed_sec = 0
        self._state = RecordingOverlayState.COUNTING
        self._light_visible = True

        use_graphic = self._canvas_progress_enabled()
        if use_graphic and not self._ensure_graphic_photos_loaded():
            _LOG.warning(
                "Recording overlay: progress graphic missing or invalid; using legacy layout"
            )
            use_graphic = False
        if use_graphic:
            self._show_canvas_layer()
            assert self._tk_photo_progress_on is not None
            self._build_canvas_items(self._tk_photo_progress_on)
            self._update_countdown_label()
            self._light_visible = True
        else:
            self._show_legacy_layer()
            self._ensure_light_packed()
            self._rebuild_circle()
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
        if (not use_graphic) or self._canvas_blink_pair_enabled():
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

    def _format_elapsed(self) -> str:
        total = max(
            0,
            min(self._elapsed_sec, self._settings.recording_duration_sec),
        )
        mm, ss = divmod(total, 60)
        return f"{mm:02d}:{ss:02d}"

    def _update_countdown_label(self) -> None:
        text = self._format_elapsed()
        if self._timer_text_id is not None and self._canvas is not None:
            try:
                self._canvas.itemconfig(self._timer_text_id, text=text)
            except tk.TclError:
                _LOG.debug("canvas timer update failed", exc_info=True)
        elif self._main_label is not None:
            self._main_label.configure(text=text)

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
        if self._state == RecordingOverlayState.SESSION_END_INFO:
            return
        if self._state != RecordingOverlayState.COUNTING:
            if self._light_canvas is not None and self._light_shape_id is not None:
                try:
                    self._light_canvas.itemconfig(
                        self._light_shape_id,
                        fill="#cc0000",
                        outline="#660000",
                    )
                except tk.TclError:
                    _LOG.debug("Recording light itemconfig failed", exc_info=True)
            return

        if self._bg_image_id is not None and self._canvas_blink_pair_enabled():
            self._light_visible = not self._light_visible
            photo = (
                self._tk_photo_progress_on
                if self._light_visible
                else self._tk_photo_progress_off
            )
            if photo is not None:
                self._set_canvas_background_photo(photo)
            self._schedule_blink()
            return

        self._light_visible = not self._light_visible
        self._apply_light_color()
        self._schedule_blink()

    def _apply_light_color(self) -> None:
        if self._light_canvas is None or self._light_shape_id is None:
            return
        fill = "#cc0000" if self._light_visible else "#330000"
        outline = "#660000" if self._light_visible else "#220000"
        try:
            self._light_canvas.itemconfig(
                self._light_shape_id,
                fill=fill,
                outline=outline,
            )
        except tk.TclError:
            _LOG.debug("Recording light apply color failed", exc_info=True)

    def _ended_hold_ms(self) -> int:
        if self._canvas_ended_enabled() and self._ensure_ended_photo_loaded():
            return self._settings.recording_ended_graphic_hold_ms
        return self._settings.recording_ended_hold_ms

    def _show_max_length_message(self) -> None:
        self._cancel_timers()
        self._cancel_ended_dismiss()
        self._state = RecordingOverlayState.ENDED_MESSAGE

        use_ended_graphic = self._canvas_ended_enabled() and self._ensure_ended_photo_loaded()
        if self._canvas_ended_enabled() and not use_ended_graphic:
            _LOG.warning(
                "Recording overlay: ended graphic missing or invalid; using legacy layout"
            )
        if use_ended_graphic:
            self._show_canvas_layer()
            assert self._tk_photo_ended is not None
            self._build_canvas_items(self._tk_photo_ended)
            self._update_countdown_label()
        else:
            self._show_legacy_layer()
            self._ensure_light_packed()
            if self._header_label is not None:
                self._header_label.pack_forget()
            if self._main_label is not None:
                self._main_label.pack_forget()
                self._main_label.configure(
                    text=self._settings.recording_ended_message,
                    font=("Arial", 16, "bold"),
                    justify="center",
                )
                self._main_label.pack(side="top", anchor="w", fill="x", expand=True)
            self._light_visible = True
            self._rebuild_square()

        self.lift()
        _LOG.info("Recording overlay: max length reached; showing ended message")
        self._root.after(50, self._grab_focus_for_dismiss_hotkey)

        self._ended_dismiss_job = self._scheduler.schedule(
            self._ended_hold_ms(),
            self._ended_dismiss_fire,
            name="recording_ended_auto_dismiss",
        )

    def _grab_focus_for_dismiss_hotkey(self) -> None:
        """After the ended slate appears, ensure the overlay receives the operator dismiss chord."""
        try:
            if self._toplevel is not None:
                self._toplevel.focus_force()
        except tk.TclError:
            _LOG.debug("recording dismiss focus grab failed", exc_info=True)

    def _show_session_end_info(self) -> None:
        self._ensure_widgets()
        self._cancel_ended_dismiss()
        self._cancel_timers()
        self._cancel_session_end_info()

        self._state = RecordingOverlayState.SESSION_END_INFO

        use_ended_graphic = self._canvas_ended_enabled() and self._ensure_ended_photo_loaded()
        if use_ended_graphic:
            self._show_canvas_layer()
            assert self._tk_photo_ended is not None
            self._build_canvas_items(self._tk_photo_ended)
            self._update_countdown_label()
        else:
            self._show_legacy_layer()
            if self._light_canvas is not None:
                try:
                    self._light_canvas.pack_forget()
                except tk.TclError:
                    _LOG.debug("Recording light pack_forget failed", exc_info=True)
            wrap = self._settings.recording_overlay_width - 100
            if self._header_label is not None:
                self._header_label.pack_forget()
            if self._main_label is not None:
                self._main_label.pack_forget()
                self._main_label.configure(
                    text=self._settings.recording_session_end_message,
                    font=("Arial", 14, "bold"),
                    justify="left",
                    wraplength=wrap,
                    anchor="w",
                )
                self._main_label.pack(side="top", anchor="w", fill="x", expand=True)

        self._session_end_info_job = self._scheduler.schedule(
            self._settings.recording_session_end_info_ms,
            self._session_end_info_fire,
            name="recording_session_end_info_dismiss",
        )

        try:
            if self._toplevel is not None:
                self._toplevel.deiconify()
        except tk.TclError:
            _LOG.debug("Recording overlay deiconify failed", exc_info=True)
        self.lift()
        _LOG.info("Recording overlay: session end info shown")

    def dismiss_ended_message(self) -> None:
        if self._state != RecordingOverlayState.ENDED_MESSAGE:
            return
        self._hide_completely()

    def dismiss_from_operator_hotkey(self) -> None:
        if self._state == RecordingOverlayState.SESSION_END_INFO:
            self._hide_completely()
            return
        if self._state not in (
            RecordingOverlayState.COUNTING,
            RecordingOverlayState.ENDED_MESSAGE,
        ):
            return
        self._show_session_end_info()

    def teardown(self) -> None:
        self._cancel_timers()
        self._cancel_ended_dismiss()
        self._cancel_session_end_info()
        if self._toplevel is not None:
            try:
                self._toplevel.destroy()
            except tk.TclError:
                _LOG.debug("Recording overlay destroy failed", exc_info=True)
            self._toplevel = None
        self._canvas = None
        self._outer = None
        self._bg_image_id = None
        self._timer_text_id = None
        self._invalidate_graphic_photos()
        self._light_canvas = None
        self._light_shape_id = None
        self._header_label = None
        self._main_label = None
        self._body_inner = None
        self._text_col = None
        self._state = RecordingOverlayState.HIDDEN

    def on_screen_resize(self, screen_width: int, screen_height: int) -> None:
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._last_applied_geometry = None
        self._invalidate_graphic_photos()
        self.lift()
