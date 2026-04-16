"""Replay buffer loading strip on the main scoreboard canvas (lower-right).

A separate top-level window tended to flash under Windows DWM when the fullscreen
scoreboard repainted. Drawing on the shared canvas avoids that. Frames are preloaded
as opaque RGB PhotoImages before the first paint so swaps do not decode on the UI tick.
"""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

from scoreboard.config.settings import Settings
from scoreboard.scheduler import AfterScheduler

_LOG = logging.getLogger(__name__)

_LOADING_FRAME_COUNT = 11
_ADVANCE_JOB_NAME = "replay_buffer_loading_hold"
_CANVAS_TAG = "replay_buffer_loading"


class ReplayBufferLoadingOverlay:
    def __init__(
        self,
        root: tk.Tk,
        settings: Settings,
        scheduler: AfterScheduler,
        canvas: tk.Canvas,
        overlay_item_id: int,
        screen_width: int,
        screen_height: int,
    ) -> None:
        self._root = root
        self._settings = settings
        self._scheduler = scheduler
        self._canvas = canvas
        self._overlay_item_id = overlay_item_id
        self._screen_w = screen_width
        self._screen_h = screen_height

        self._hold_job: str | None = None
        self._hint_win: tk.Toplevel | None = None
        self._hint_job: str | None = None
        self._photos: list[ImageTk.PhotoImage] = []
        self._frame_index = 0
        self._item_a: int | None = None
        self._item_b: int | None = None
        self._sequence_active = False

    def sync_canvas_stack(self) -> None:
        """Call after raising the transparent overlay so this strip stays visible on top."""
        if not self._sequence_active:
            return
        try:
            self._canvas.tag_raise(_CANVAS_TAG, self._overlay_item_id)
        except tk.TclError:
            _LOG.debug("replay buffer loading: tag_raise failed", exc_info=True)

    def teardown(self) -> None:
        self._dismiss_hint()
        self._cancel_hold()
        self._destroy_canvas_items()
        self._photos.clear()

    def start_sequence(self) -> None:
        _LOG.info("Replay buffer loading: hotkey received")
        paths = self._resolve_frame_paths()
        if paths is None:
            self._flash_missing_assets_message()
            return

        photos: list[ImageTk.PhotoImage] = []
        for p in paths:
            photo = _path_to_rgb_photo(p)
            if photo is None:
                _LOG.error("Replay buffer loading: failed to load %s", p)
                return
            photos.append(photo)

        self._cancel_hold()
        self._destroy_canvas_items()
        self._photos = photos
        self._frame_index = 0

        m = max(0, self._settings.replay_buffer_loading_margin_px)
        cx = max(0, self._screen_w - m)
        cy = max(0, self._screen_h - m)

        first = self._photos[0]
        try:
            self._item_a = self._canvas.create_image(
                cx,
                cy,
                anchor="se",
                image=first,
                tags=_CANVAS_TAG,
            )
            self._item_b = self._canvas.create_image(
                cx,
                cy,
                anchor="se",
                image=first,
                tags=_CANVAS_TAG,
                state="hidden",
            )
        except tk.TclError:
            _LOG.exception("Replay buffer loading: canvas create_image failed")
            self._destroy_canvas_items()
            self._photos.clear()
            return

        self._sequence_active = True
        self.sync_canvas_stack()
        self._paint_frame(0)
        self._schedule_hold()

    def _png_for_frame(self, base: Path, index: int) -> Path | None:
        exact = base / f"Loading{index:02d}.png"
        if exact.is_file():
            return exact
        stem = f"Loading{index:02d}"
        try:
            for f in base.iterdir():
                if not f.is_file() or f.suffix.lower() != ".png":
                    continue
                if f.stem.lower() == stem.lower():
                    return f
        except OSError:
            _LOG.debug("replay buffer loading: could not list %s", base, exc_info=True)
        return None

    def _resolve_frame_paths(self) -> list[str] | None:
        base = Path(self._settings.replay_buffer_loading_dir)
        if not base.is_dir():
            _LOG.error(
                "Replay buffer loading: folder missing or not a directory: %s",
                base.resolve(),
            )
            return None
        out: list[str] = []
        for i in range(1, _LOADING_FRAME_COUNT + 1):
            p = self._png_for_frame(base, i)
            if p is None:
                _LOG.error(
                    "Replay buffer loading: need %s/Loading%02d.png (and Loading02 … Loading%02d)",
                    base.resolve(),
                    i,
                    _LOADING_FRAME_COUNT,
                )
                return None
            out.append(str(p.resolve()))
        return out

    def _dismiss_hint(self) -> None:
        self._scheduler.cancel(self._hint_job)
        self._hint_job = None
        if self._hint_win is not None:
            try:
                self._hint_win.destroy()
            except tk.TclError:
                pass
            self._hint_win = None

    def _flash_missing_assets_message(self) -> None:
        self._dismiss_hint()
        base = Path(self._settings.replay_buffer_loading_dir).resolve()
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            _LOG.debug("replay buffer loading hint: topmost failed", exc_info=True)
        frame = tk.Frame(win, bg="#2a1111", highlightthickness=1, highlightbackground="#884444")
        tk.Label(
            frame,
            text=(
                "Replay buffer loading graphics not found.\n\n"
                f"Add Loading01.png through Loading11.png to:\n{base}"
            ),
            fg="#ffcccc",
            bg="#2a1111",
            font=("Segoe UI", 11),
            justify="left",
            padx=14,
            pady=12,
        ).pack()
        frame.pack()
        win.update_idletasks()
        fw = max(320, win.winfo_reqwidth())
        fh = max(80, win.winfo_reqheight())
        sw = max(1, self._root.winfo_screenwidth())
        sh = max(1, self._root.winfo_screenheight())
        m = max(0, self._settings.replay_buffer_loading_margin_px)
        x = max(0, sw - fw - m)
        y = max(0, sh - fh - m)
        win.geometry(f"{fw}x{fh}+{x}+{y}")
        self._hint_win = win

        def _close_hint() -> None:
            self._hint_job = None
            self._dismiss_hint()

        self._hint_job = self._scheduler.schedule(
            8000,
            _close_hint,
            name="replay_buffer_loading_hint",
        )

    def _cancel_hold(self) -> None:
        self._scheduler.cancel(self._hold_job)
        self._hold_job = None

    def _destroy_canvas_items(self) -> None:
        self._sequence_active = False
        self._item_a = None
        self._item_b = None
        try:
            self._canvas.delete(_CANVAS_TAG)
        except tk.TclError:
            pass

    def _paint_frame(self, idx: int) -> None:
        if self._item_a is None or self._item_b is None or not self._photos:
            return
        photo = self._photos[idx]
        try:
            if idx == 0:
                self._canvas.itemconfig(self._item_a, image=photo, state="normal")
                self._canvas.itemconfig(self._item_b, state="hidden")
            elif idx % 2 == 1:
                self._canvas.itemconfig(self._item_b, image=photo)
                self._canvas.itemconfig(self._item_a, state="hidden")
                self._canvas.itemconfig(self._item_b, state="normal")
            else:
                self._canvas.itemconfig(self._item_a, image=photo)
                self._canvas.itemconfig(self._item_b, state="hidden")
                self._canvas.itemconfig(self._item_a, state="normal")
            self.sync_canvas_stack()
        except tk.TclError:
            _LOG.exception("Replay buffer loading: canvas itemconfig failed")
            self._finish_sequence()

    def _schedule_hold(self) -> None:
        self._cancel_hold()
        self._hold_job = self._scheduler.schedule(
            self._settings.replay_buffer_loading_frame_ms,
            self._after_hold,
            name=_ADVANCE_JOB_NAME,
        )

    def _after_hold(self) -> None:
        self._hold_job = None
        n = len(self._photos)
        if n == 0:
            self._finish_sequence()
            return
        if self._frame_index >= n - 1:
            self._finish_sequence()
            return
        self._frame_index += 1
        self._paint_frame(self._frame_index)
        self._schedule_hold()

    def _finish_sequence(self) -> None:
        self._cancel_hold()
        self._destroy_canvas_items()
        self._photos.clear()


def _path_to_rgb_photo(path: str) -> ImageTk.PhotoImage | None:
    """Flatten RGBA onto black so Tk does less alpha blending when swapping PhotoImages."""
    try:
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, (0, 0, 0))
        rgb.paste(rgba, mask=rgba.split()[3])
        return ImageTk.PhotoImage(rgb)
    except OSError:
        return None
