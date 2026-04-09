import tkinter as tk
from PIL import Image, ImageTk
import os
import json
import random
import shutil
import subprocess
import tempfile
import time

STATE_FILE = "state.json"
ENV_FILE = ".env"
IDLE_TIMEOUT_MS = 30 * 60 * 1000
SLIDESHOW_INTERVAL_MS = 12 * 1000
SLIDESHOW_FADE_DURATION_MS = 1000
SLIDESHOW_FADE_STEPS = 10
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
DEFAULT_REPLAY_VIDEO_PATH = r"C:\ReplayTrove\INSTANTREPLAY.mp4"
REPLAY_VIDEO_START_DELAY_MS = 3000
REPLAY_VIDEO_POLL_MS = 500
REPLAY_RETURN_SLATE_HOLD_MS = 350
REPLAY_TO_VIDEO_FADE_DURATION_MS = 500
REPLAY_TO_VIDEO_FADE_STEPS = 10


def env_truthy(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_env_value(key, default=None):
    if not os.path.exists(ENV_FILE):
        return default

    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                env_key, env_value = line.split("=", 1)
                if env_key.strip() == key:
                    return env_value.strip().strip('"').strip("'")
    except Exception:
        return default

    return default


class ScoreboardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Scoreboard")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")

        self.showing_replay = False
        self.is_transitioning = False
        self.replay_video_process = None
        self.replay_video_active = False
        self.video_host_visible = False
        self.replay_video_start_job_id = None
        self.replay_video_poll_job_id = None
        self.screensaver_active = False
        self.screensaver_job_id = None
        self.screensaver_fade_job_id = None
        self.idle_check_job_id = None
        self.last_input_ms = int(time.monotonic() * 1000)
        self.slideshow_dir = load_env_value(
            "SLIDESHOW_DIR",
            r"C:\Users\admin\Dropbox\slideshow"
        )
        self.replay_video_path = load_env_value(
            "REPLAY_VIDEO_PATH",
            DEFAULT_REPLAY_VIDEO_PATH
        )
        self.mpv_path = load_env_value("MPV_PATH")
        self.mpv_exit_hotkey = load_env_value("MPV_EXIT_HOTKEY", "Ctrl+Alt+q")
        self.mpv_embedded = env_truthy(load_env_value("MPV_EMBEDDED"), False)
        self.mpv_input_conf_path = None
        self.current_screensaver_photo = None
        self.current_screensaver_frame = None

        # Load state
        self.score_a = 0
        self.score_b = 0
        self.load_state()

        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()

        # Load images
        self.bg_image = Image.open("Score BG.png").resize(
            (self.screen_width, self.screen_height)
        ).convert("RGBA")

        self.replay_image = Image.open("ir slate.png").resize(
            (self.screen_width, self.screen_height)
        ).convert("RGBA")

        self.bg_photo = ImageTk.PhotoImage(self.bg_image)

        # Transparent overlay to start
        self.transparent_overlay = Image.new(
            "RGBA",
            (self.screen_width, self.screen_height),
            (0, 0, 0, 0)
        )
        self.overlay_photo = ImageTk.PhotoImage(self.transparent_overlay)

        self.canvas = tk.Canvas(
            root,
            width=self.screen_width,
            height=self.screen_height,
            highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.video_host = tk.Frame(root, bg="black")
        self.ensure_window_opaque()

        # Base scoreboard background
        self.bg_canvas = self.canvas.create_image(
            0, 0, image=self.bg_photo, anchor="nw"
        )

        # Overlay layer for replay slate
        self.overlay_canvas = self.canvas.create_image(
            0, 0, image=self.overlay_photo, anchor="nw"
        )

        # Keep references alive
        self.current_overlay_photo = self.overlay_photo
        self.fade_frames = []

        # Positioning
        self.left_x = int(self.screen_width * 0.23)
        self.right_x = int(self.screen_width * 0.77)
        self.center_y = int(self.screen_height * 0.51)

        # Font size
        self.font_size = int(self.screen_height * 0.45)

        # Horizontal squeeze
        self.squeeze_x = 0.88

        # Draw initial scores
        self.draw_scores()

        # Ensure overlay stays above scores/background
        self.canvas.tag_raise(self.overlay_canvas)

        # Key bindings
        root.bind("q", lambda e: self.on_streamdeck_input(
            lambda: self.update_score("a", 1)
        ))
        root.bind("a", lambda e: self.on_streamdeck_input(
            lambda: self.update_score("a", -1)
        ))
        root.bind("p", lambda e: self.on_streamdeck_input(
            lambda: self.update_score("b", 1)
        ))
        root.bind("l", lambda e: self.on_streamdeck_input(
            lambda: self.update_score("b", -1)
        ))
        root.bind("r", lambda e: self.on_streamdeck_input(self.reset_scores))
        root.bind("i", lambda e: self.on_streamdeck_input(self.toggle_replay))
        root.bind("<Escape>", lambda e: self.close_app())

        self.schedule_idle_check()

    def on_streamdeck_input(self, action):
        self.last_input_ms = int(time.monotonic() * 1000)

        if self.screensaver_active:
            self.stop_screensaver()
            return

        action()

    def close_app(self):
        self.clear_pending_screensaver_jobs()
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()
        self.stop_replay_video_process()
        self.hide_video_host()
        self.cleanup_mpv_input_conf()
        self.root.destroy()

    def schedule_idle_check(self):
        if self.idle_check_job_id is not None:
            self.root.after_cancel(self.idle_check_job_id)

        self.idle_check_job_id = self.root.after(5000, self.check_idle_timeout)

    def check_idle_timeout(self):
        self.idle_check_job_id = None

        now_ms = int(time.monotonic() * 1000)
        idle_ms = now_ms - self.last_input_ms

        if (
            not self.screensaver_active
            and not self.showing_replay
            and not self.is_transitioning
            and idle_ms >= IDLE_TIMEOUT_MS
        ):
            self.start_screensaver()

        self.schedule_idle_check()

    def get_slideshow_images(self):
        if not self.slideshow_dir or not os.path.isdir(self.slideshow_dir):
            return []

        files = []
        try:
            for filename in os.listdir(self.slideshow_dir):
                path = os.path.join(self.slideshow_dir, filename)
                if (
                    os.path.isfile(path)
                    and filename.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS)
                ):
                    files.append(path)
        except Exception:
            return []

        return files

    def load_and_cover_image(self, image_path):
        with Image.open(image_path) as img:
            source = img.convert("RGBA")

        src_w, src_h = source.size
        if src_w == 0 or src_h == 0:
            return None

        scale = max(self.screen_width / src_w, self.screen_height / src_h)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)

        resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)

        crop_x = max(0, (new_w - self.screen_width) // 2)
        crop_y = max(0, (new_h - self.screen_height) // 2)

        return resized.crop(
            (
                crop_x,
                crop_y,
                crop_x + self.screen_width,
                crop_y + self.screen_height
            )
        )

    def clear_pending_screensaver_jobs(self):
        if self.screensaver_job_id is not None:
            self.root.after_cancel(self.screensaver_job_id)
            self.screensaver_job_id = None

        if self.screensaver_fade_job_id is not None:
            self.root.after_cancel(self.screensaver_fade_job_id)
            self.screensaver_fade_job_id = None

    def start_screensaver(self):
        if self.screensaver_active:
            return

        self.screensaver_active = True
        self.current_screensaver_frame = None
        self.show_next_screensaver_image()

    def stop_screensaver(self):
        if not self.screensaver_active:
            return

        self.screensaver_active = False
        self.clear_pending_screensaver_jobs()

        self.current_screensaver_photo = self.overlay_photo
        self.current_screensaver_frame = None
        self.canvas.itemconfig(self.overlay_canvas, image=self.current_screensaver_photo)
        self.canvas.tag_raise(self.overlay_canvas)

    def show_next_screensaver_image(self):
        if not self.screensaver_active:
            return

        image_paths = self.get_slideshow_images()
        if not image_paths:
            self.screensaver_job_id = self.root.after(
                SLIDESHOW_INTERVAL_MS,
                self.show_next_screensaver_image
            )
            return

        selected_path = random.choice(image_paths)

        try:
            next_frame = self.load_and_cover_image(selected_path)
            if next_frame is None:
                raise ValueError("Invalid image dimensions")

            if self.current_screensaver_frame is None:
                self.fade_screensaver_in(next_frame)
            else:
                self.fade_between_screensaver_images(
                    self.current_screensaver_frame,
                    next_frame
                )
        except Exception:
            self.screensaver_job_id = self.root.after(
                SLIDESHOW_INTERVAL_MS,
                self.show_next_screensaver_image
            )

    def fade_screensaver_in(self, next_frame):
        frames = []
        for i in range(SLIDESHOW_FADE_STEPS + 1):
            alpha = int(255 * (i / SLIDESHOW_FADE_STEPS))
            frame = next_frame.copy()
            frame.putalpha(alpha)
            frames.append(ImageTk.PhotoImage(frame))

        delay = max(1, SLIDESHOW_FADE_DURATION_MS // SLIDESHOW_FADE_STEPS)
        self.animate_screensaver_frames(
            frames=frames,
            delay=delay,
            on_complete=lambda: self.finish_screensaver_frame(next_frame)
        )

    def fade_between_screensaver_images(self, from_frame, to_frame):
        frames = []
        for i in range(SLIDESHOW_FADE_STEPS + 1):
            blend_amount = i / SLIDESHOW_FADE_STEPS
            blended = Image.blend(from_frame, to_frame, blend_amount)
            frames.append(ImageTk.PhotoImage(blended))

        delay = max(1, SLIDESHOW_FADE_DURATION_MS // SLIDESHOW_FADE_STEPS)
        self.animate_screensaver_frames(
            frames=frames,
            delay=delay,
            on_complete=lambda: self.finish_screensaver_frame(to_frame)
        )

    def animate_screensaver_frames(self, frames, delay, on_complete, index=0):
        if not self.screensaver_active:
            return

        if index >= len(frames):
            self.screensaver_fade_job_id = None
            on_complete()
            return

        self.current_screensaver_photo = frames[index]
        self.canvas.itemconfig(self.overlay_canvas, image=self.current_screensaver_photo)
        self.canvas.tag_raise(self.overlay_canvas)

        self.screensaver_fade_job_id = self.root.after(
            delay,
            lambda: self.animate_screensaver_frames(
                frames,
                delay,
                on_complete,
                index + 1
            )
        )

    def finish_screensaver_frame(self, frame):
        if not self.screensaver_active:
            return

        self.current_screensaver_frame = frame
        self.screensaver_job_id = self.root.after(
            SLIDESHOW_INTERVAL_MS,
            self.show_next_screensaver_image
        )

    def create_scaled_text(self, x, y, text, color):
        item = self.canvas.create_text(
            x,
            y,
            text=text,
            fill=color,
            font=("Arial", self.font_size, "bold"),
            tags="score"
        )
        self.canvas.scale(item, x, y, self.squeeze_x, 1.0)
        return item

    def draw_text_with_effects(self, x, y, text):
        items = []

        shadow_offset = int(self.font_size * 0.03)
        outline_offset = int(self.font_size * 0.015)

        items.append(
            self.create_scaled_text(
                x + shadow_offset,
                y + shadow_offset,
                text,
                "#000000"
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
                        "#000000"
                    )
                )

        items.append(
            self.create_scaled_text(
                x,
                y,
                text,
                "#FFFFFF"
            )
        )

        return items

    def draw_scores(self):
        self.canvas.delete("score")

        self.score_a_items = self.draw_text_with_effects(
            self.left_x, self.center_y, str(self.score_a)
        )
        self.score_b_items = self.draw_text_with_effects(
            self.right_x, self.center_y, str(self.score_b)
        )

        # Keep overlay on top
        self.canvas.tag_raise(self.overlay_canvas)

    def update_score(self, team, delta):
        if self.showing_replay or self.is_transitioning:
            return

        if team == "a":
            self.score_a = max(0, min(99, self.score_a + delta))
        else:
            self.score_b = max(0, min(99, self.score_b + delta))

        self.draw_scores()
        self.save_state()

    def reset_scores(self):
        if self.showing_replay or self.is_transitioning:
            return

        self.score_a = 0
        self.score_b = 0
        self.draw_scores()
        self.save_state()

    def toggle_replay(self):
        if self.is_transitioning:
            return

        if self.replay_video_active:
            self.stop_replay_video_and_return()
            return

        if self.showing_replay:
            self.cancel_replay_video_launch()
            self.fade_overlay_out()
        else:
            self.fade_overlay_in()

    def fade_overlay_in(self):
        self.is_transitioning = True
        self.run_overlay_fade(
            start_alpha=0,
            end_alpha=255,
            steps=8,
            delay=15,
            on_complete=self.finish_fade_in
        )

    def finish_fade_in(self):
        self.showing_replay = True
        self.is_transitioning = False
        self.schedule_replay_video_launch()

    def fade_overlay_out(self):
        self.is_transitioning = True
        self.run_overlay_fade(
            start_alpha=255,
            end_alpha=0,
            steps=10,
            delay=20,
            on_complete=self.finish_fade_out
        )

    def finish_fade_out(self):
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()
        self.stop_replay_video_process()
        self.hide_video_host()
        self.showing_replay = False
        self.replay_video_active = False
        self.is_transitioning = False

    def run_overlay_fade(self, start_alpha, end_alpha, steps, delay, on_complete):
        self.fade_frames = []

        for i in range(steps + 1):
            alpha = start_alpha + (end_alpha - start_alpha) * (i / steps)
            frame = self.replay_image.copy()
            frame.putalpha(int(alpha))
            photo = ImageTk.PhotoImage(frame)
            self.fade_frames.append(photo)

        self.animate_overlay_fade(0, delay, on_complete)

    def animate_overlay_fade(self, index, delay, on_complete):
        if index >= len(self.fade_frames):
            self.fade_frames = []
            on_complete()
            return

        self.current_overlay_photo = self.fade_frames[index]
        self.canvas.itemconfig(self.overlay_canvas, image=self.current_overlay_photo)
        self.canvas.tag_raise(self.overlay_canvas)

        self.root.after(
            delay,
            lambda: self.animate_overlay_fade(index + 1, delay, on_complete)
        )

    def schedule_replay_video_launch(self):
        self.cancel_replay_video_launch()
        self.replay_video_start_job_id = self.root.after(
            REPLAY_VIDEO_START_DELAY_MS,
            self.start_replay_video
        )

    def cancel_replay_video_launch(self):
        if self.replay_video_start_job_id is not None:
            self.root.after_cancel(self.replay_video_start_job_id)
            self.replay_video_start_job_id = None

    def start_replay_video(self):
        self.replay_video_start_job_id = None

        if not self.showing_replay or self.replay_video_active:
            return

        if not self.replay_video_path or not os.path.isfile(self.replay_video_path):
            return

        mpv_executable = self.resolve_mpv_executable()
        if mpv_executable is None:
            return

        input_conf_path = self.ensure_mpv_input_conf()
        if input_conf_path is None:
            return

        self.prepare_canvas_for_video_transition()

        if self.mpv_embedded:
            self.show_video_host()
            self.root.update_idletasks()
            self.root.after(250, lambda: self.spawn_mpv_embedded(mpv_executable, input_conf_path))
        else:
            self.spawn_mpv_fullscreen(mpv_executable, input_conf_path)

    def spawn_mpv_fullscreen(self, mpv_executable, input_conf_path):
        if not self.showing_replay or self.replay_video_active:
            return

        try:
            self.replay_video_process = subprocess.Popen(
                [
                    mpv_executable,
                    "--fs",
                    "--force-window=yes",
                    "--keep-open=yes",
                    "--ontop",
                    "--no-input-terminal",
                    f"--input-conf={input_conf_path}",
                    self.replay_video_path
                ]
            )
        except Exception:
            self.replay_video_process = None
            self.restore_canvas_after_video()
            return

        self.replay_video_active = True
        self.fade_replay_slate_to_video()
        self.schedule_replay_video_poll()

    def spawn_mpv_embedded(self, mpv_executable, input_conf_path):
        if not self.showing_replay or self.replay_video_active:
            self.hide_video_host()
            return

        self.root.update_idletasks()
        host_id = self.video_host.winfo_id()

        try:
            self.replay_video_process = subprocess.Popen(
                [
                    mpv_executable,
                    f"--wid={host_id}",
                    "--no-border",
                    "--keep-open=yes",
                    "--no-input-terminal",
                    "--hwdec=no",
                    f"--input-conf={input_conf_path}",
                    self.replay_video_path
                ]
            )
        except Exception:
            self.replay_video_process = None
            self.hide_video_host()
            self.restore_canvas_after_video()
            return

        self.replay_video_active = True
        self.fade_replay_slate_to_video()
        self.schedule_replay_video_poll()

    def prepare_canvas_for_video_transition(self):
        # Keep only replay overlay visible while we transition to video.
        self.canvas.configure(bg="black")
        self.canvas.itemconfig(self.bg_canvas, state="hidden")
        self.canvas.itemconfig("score", state="hidden")

    def restore_canvas_after_video(self):
        self.canvas.configure(bg="black")
        self.canvas.itemconfig(self.bg_canvas, state="normal")
        self.canvas.itemconfig("score", state="normal")
        self.draw_scores()

    def show_video_host(self):
        if self.video_host_visible:
            return

        self.video_host.place(x=0, y=0, relwidth=1, relheight=1)
        self.video_host_visible = True
        self.canvas.lift()

    def hide_video_host(self):
        if not self.video_host_visible:
            return

        self.video_host.place_forget()
        self.video_host_visible = False

    def hide_canvas_for_video_playback(self):
        self.canvas.pack_forget()

    def show_canvas_after_video(self):
        self.canvas.pack(fill="both", expand=True)
        self.canvas.tag_raise(self.overlay_canvas)
        self.ensure_window_opaque()

    def fade_replay_slate_to_video(self):
        delay = max(1, REPLAY_TO_VIDEO_FADE_DURATION_MS // REPLAY_TO_VIDEO_FADE_STEPS)
        self.run_overlay_fade(
            start_alpha=255,
            end_alpha=0,
            steps=REPLAY_TO_VIDEO_FADE_STEPS,
            delay=delay,
            on_complete=self.finish_replay_slate_to_video
        )

    def finish_replay_slate_to_video(self):
        self.ensure_window_opaque()
        self.current_overlay_photo = self.overlay_photo
        self.canvas.itemconfig(self.overlay_canvas, image=self.current_overlay_photo)
        self.hide_canvas_for_video_playback()

    def ensure_window_opaque(self):
        try:
            self.root.attributes("-alpha", 1.0)
        except Exception:
            pass

    def ensure_mpv_input_conf(self):
        hotkey = (self.mpv_exit_hotkey or "").strip()
        if not hotkey:
            hotkey = "Ctrl+Alt+q"

        conf_line = f"{hotkey} quit\n"

        try:
            temp_dir = tempfile.gettempdir()
            conf_path = os.path.join(temp_dir, "scoreboard_mpv_input.conf")
            with open(conf_path, "w", encoding="utf-8") as f:
                f.write(conf_line)
            self.mpv_input_conf_path = conf_path
            return conf_path
        except Exception:
            self.mpv_input_conf_path = None
            return None

    def cleanup_mpv_input_conf(self):
        if not self.mpv_input_conf_path:
            return

        try:
            if os.path.isfile(self.mpv_input_conf_path):
                os.remove(self.mpv_input_conf_path)
        except Exception:
            pass

        self.mpv_input_conf_path = None

    def resolve_mpv_executable(self):
        candidates = []

        if self.mpv_path:
            candidates.append(self.mpv_path)

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
                r"C:\mpv\mpv.exe"
            ]
        )

        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate

        return None

    def stop_replay_video_and_return(self):
        self.cancel_replay_video_launch()
        self.cancel_replay_video_poll()
        self.stop_replay_video_process()
        self.replay_video_active = False

        if self.showing_replay and not self.is_transitioning:
            self.hide_video_host()
            self.show_canvas_after_video()
            self.restore_canvas_after_video()
            self.current_overlay_photo = ImageTk.PhotoImage(self.replay_image)
            self.canvas.itemconfig(self.overlay_canvas, image=self.current_overlay_photo)
            self.canvas.tag_raise(self.overlay_canvas)
            self.root.after(REPLAY_RETURN_SLATE_HOLD_MS, self.fade_overlay_out)

    def stop_replay_video_process(self):
        if self.replay_video_process is None:
            return

        process = self.replay_video_process
        self.replay_video_process = None

        if process.poll() is not None:
            return

        try:
            process.terminate()
            process.wait(timeout=1.5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def schedule_replay_video_poll(self):
        self.cancel_replay_video_poll()
        self.replay_video_poll_job_id = self.root.after(
            REPLAY_VIDEO_POLL_MS,
            self.poll_replay_video_process
        )

    def cancel_replay_video_poll(self):
        if self.replay_video_poll_job_id is not None:
            self.root.after_cancel(self.replay_video_poll_job_id)
            self.replay_video_poll_job_id = None

    def poll_replay_video_process(self):
        self.replay_video_poll_job_id = None

        process = self.replay_video_process
        if not self.replay_video_active or process is None:
            return

        if process.poll() is None:
            self.schedule_replay_video_poll()
            return

        self.replay_video_process = None
        self.replay_video_active = False
        if self.showing_replay and not self.is_transitioning:
            self.hide_video_host()
            self.show_canvas_after_video()
            self.restore_canvas_after_video()
            self.current_overlay_photo = ImageTk.PhotoImage(self.replay_image)
            self.canvas.itemconfig(self.overlay_canvas, image=self.current_overlay_photo)
            self.canvas.tag_raise(self.overlay_canvas)
            self.root.after(REPLAY_RETURN_SLATE_HOLD_MS, self.fade_overlay_out)

    def save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(
                {
                    "score_a": self.score_a,
                    "score_b": self.score_b
                },
                f
            )

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.score_a = data.get("score_a", 0)
                    self.score_b = data.get("score_b", 0)
            except Exception:
                pass


if __name__ == "__main__":
    root = tk.Tk()
    app = ScoreboardApp(root)
    root.mainloop()