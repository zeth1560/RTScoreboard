"""Idle slideshow screensaver: image discovery, cover scaling, fades, scheduling."""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Callable

import tkinter as tk
from PIL import Image, ImageTk

from scoreboard.config.settings import Settings, SUPPORTED_IMAGE_EXTENSIONS
from scoreboard.scheduler import AfterScheduler, JobGroup

_LOG = logging.getLogger(__name__)

# While slideshow is up, periodically bring the Tk window forward so Stream Deck / hotkeys
# still reach this app after the focus watchdog has stopped (~few minutes post-start).
_SCREENSAVER_FOCUS_RECLAIM_MS = 2500


class Screensaver:
    def __init__(
        self,
        root: tk.Tk,
        canvas: tk.Canvas,
        overlay_canvas_id: int,
        settings: Settings,
        scheduler: AfterScheduler,
        screen_width: int,
        screen_height: int,
        lift_recording_overlay: Callable[[], None],
        reclaim_keyboard_focus: Callable[[], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
        after_overlay_raise: Callable[[], None] | None = None,
        on_active_changed: Callable[[bool], None] | None = None,
    ) -> None:
        self._root = root
        self._canvas = canvas
        self._overlay_canvas_id = overlay_canvas_id
        self._settings = settings
        self._scheduler = scheduler
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._lift_recording_overlay = lift_recording_overlay
        self._reclaim_keyboard_focus = reclaim_keyboard_focus
        self._on_stopped = on_stopped
        self._after_overlay_raise = after_overlay_raise
        self._on_active_changed = on_active_changed

        self._active = False
        self._jobs = JobGroup(scheduler)
        self._focus_reclaim_job: str | None = None
        self._transparent_overlay_photo: ImageTk.PhotoImage | None = None
        self._current_photo: ImageTk.PhotoImage | None = None
        self._current_frame: Image.Image | None = None
        self._fade_frames_hold: list[ImageTk.PhotoImage] = []
        self._last_slideshow_path: str | None = None

    def set_transparent_overlay_photo(self, photo: ImageTk.PhotoImage) -> None:
        self._transparent_overlay_photo = photo

    def is_active(self) -> bool:
        return self._active

    def on_screen_resize(self, width: int, height: int) -> None:
        self._screen_width = width
        self._screen_height = height

    def _clear_jobs(self) -> None:
        self._scheduler.cancel(self._focus_reclaim_job)
        self._focus_reclaim_job = None
        self._jobs.cancel_all()

    def _focus_reclaim_tick(self) -> None:
        self._focus_reclaim_job = None
        if not self._active or self._reclaim_keyboard_focus is None:
            return
        try:
            self._reclaim_keyboard_focus()
        except (RuntimeError, tk.TclError, ValueError, TypeError):
            _LOG.debug("Screensaver: focus reclaim failed", exc_info=True)
        self._focus_reclaim_job = self._scheduler.schedule(
            _SCREENSAVER_FOCUS_RECLAIM_MS,
            self._focus_reclaim_tick,
            name="screensaver_focus_reclaim",
            background_resilience=True,
        )

    def stop(self) -> None:
        if not self._active:
            return
        _LOG.info("Screensaver: stopping")
        self._active = False
        self._clear_jobs()
        self._last_slideshow_path = None
        if self._transparent_overlay_photo is not None:
            self._current_photo = self._transparent_overlay_photo
            self._current_frame = None
            self._canvas.itemconfig(
                self._overlay_canvas_id,
                image=self._current_photo,
            )
            self._canvas.tag_raise(self._overlay_canvas_id)
            if self._after_overlay_raise is not None:
                self._after_overlay_raise()
            self._lift_recording_overlay()
        if self._on_stopped is not None:
            try:
                self._on_stopped()
            except Exception:
                _LOG.exception("Screensaver: on_stopped callback failed")
        self._notify_active_changed(False)

    def start(self) -> None:
        if not self._settings.slideshow_enabled:
            _LOG.debug("Screensaver: start ignored (SLIDESHOW_ENABLED=0)")
            return
        if self._active:
            return
        _LOG.info("Screensaver: starting")
        self._active = True
        self._notify_active_changed(True)
        self._current_frame = None
        if self._reclaim_keyboard_focus is not None:
            self._focus_reclaim_tick()
        self._show_next_image()

    def get_slideshow_images(self) -> list[str]:
        d = self._settings.slideshow_dir
        if not d or not os.path.isdir(d):
            return []
        files: list[str] = []
        try:
            for filename in os.listdir(d):
                path = os.path.join(d, filename)
                if os.path.isfile(path) and filename.lower().endswith(
                    SUPPORTED_IMAGE_EXTENSIONS
                ):
                    files.append(path)
        except OSError:
            _LOG.exception("Screensaver: failed to list slideshow directory %s", d)
            return []
        return files

    def load_and_cover_image(self, image_path: str) -> Image.Image | None:
        try:
            with Image.open(image_path) as img:
                source = img.convert("RGBA")
        except OSError:
            _LOG.warning("Screensaver: could not open image %s", image_path, exc_info=True)
            return None

        src_w, src_h = source.size
        if src_w == 0 or src_h == 0:
            return None

        scale = max(self._screen_width / src_w, self._screen_height / src_h)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)

        resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)

        crop_x = max(0, (new_w - self._screen_width) // 2)
        crop_y = max(0, (new_h - self._screen_height) // 2)

        return resized.crop(
            (
                crop_x,
                crop_y,
                crop_x + self._screen_width,
                crop_y + self._screen_height,
            )
        )

    def _show_next_image(self) -> None:
        if not self._active:
            return

        paths = self.get_slideshow_images()
        if not paths:
            self._jobs.schedule(
                self._settings.slideshow_interval_ms,
                self._show_next_image,
                name="screensaver_retry_empty",
            )
            return

        if len(paths) > 1 and self._last_slideshow_path is not None:
            candidates = [p for p in paths if p != self._last_slideshow_path]
            pool = candidates if candidates else paths
        else:
            pool = paths
        selected = random.choice(pool)
        try:
            next_frame = self.load_and_cover_image(selected)
            if next_frame is None:
                raise ValueError("Invalid image dimensions")

            if self._current_frame is None:
                self._fade_in(next_frame)
            else:
                self._fade_between(self._current_frame, next_frame)
            self._last_slideshow_path = selected
        except Exception:
            _LOG.exception("Screensaver: error loading %s", selected)
            self._jobs.schedule(
                self._settings.slideshow_interval_ms,
                self._show_next_image,
                name="screensaver_retry_after_error",
            )

    def _fade_in(self, next_frame: Image.Image) -> None:
        steps = self._settings.slideshow_fade_steps
        frames: list[ImageTk.PhotoImage] = []
        for i in range(steps + 1):
            alpha = int(255 * (i / steps))
            frame = next_frame.copy()
            frame.putalpha(alpha)
            frames.append(ImageTk.PhotoImage(frame))

        self._fade_frames_hold = frames
        delay = max(1, self._settings.slideshow_fade_duration_ms // steps)
        self._animate_frames(frames, delay, lambda: self._finish_frame(next_frame))

    def _fade_between(self, from_frame: Image.Image, to_frame: Image.Image) -> None:
        steps = self._settings.slideshow_fade_steps
        frames: list[ImageTk.PhotoImage] = []
        for i in range(steps + 1):
            blend_amount = i / steps
            blended = Image.blend(from_frame, to_frame, blend_amount)
            frames.append(ImageTk.PhotoImage(blended))

        self._fade_frames_hold = frames
        delay = max(1, self._settings.slideshow_fade_duration_ms // steps)
        self._animate_frames(frames, delay, lambda: self._finish_frame(to_frame))

    def _animate_frames(
        self,
        frames: list[ImageTk.PhotoImage],
        delay: int,
        on_complete: Callable[[], None],
        index: int = 0,
    ) -> None:
        if not self._active:
            return

        if index >= len(frames):
            on_complete()
            return

        self._current_photo = frames[index]
        self._canvas.itemconfig(
            self._overlay_canvas_id,
            image=self._current_photo,
        )
        self._canvas.tag_raise(self._overlay_canvas_id)
        if self._after_overlay_raise is not None:
            self._after_overlay_raise()
        self._lift_recording_overlay()

        self._jobs.schedule(
            delay,
            lambda: self._animate_frames(frames, delay, on_complete, index + 1),
            name="screensaver_fade_step",
        )

    def _finish_frame(self, frame: Image.Image) -> None:
        if not self._active:
            return
        self._current_frame = frame
        self._jobs.schedule(
            self._settings.slideshow_interval_ms,
            self._show_next_image,
            name="screensaver_next_slide",
        )

    def _notify_active_changed(self, active: bool) -> None:
        if self._on_active_changed is None:
            return
        try:
            self._on_active_changed(active)
        except Exception:
            _LOG.exception("Screensaver: on_active_changed failed")

    def teardown(self) -> None:
        was = self._active
        self._clear_jobs()
        self._active = False
        self._fade_frames_hold.clear()
        self._last_slideshow_path = None
        if was:
            self._notify_active_changed(False)
