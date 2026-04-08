import tkinter as tk
from PIL import Image, ImageTk
import os
import json

STATE_FILE = "state.json"


class ScoreboardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Scoreboard")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")

        self.showing_replay = False
        self.is_transitioning = False

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
        self.center_y = int(self.screen_height * 0.58)

        # Font size
        self.font_size = int(self.screen_height * 0.45)

        # Horizontal squeeze
        self.squeeze_x = 0.88

        # Draw initial scores
        self.draw_scores()

        # Ensure overlay stays above scores/background
        self.canvas.tag_raise(self.overlay_canvas)

        # Key bindings
        root.bind("q", lambda e: self.update_score("a", 1))
        root.bind("a", lambda e: self.update_score("a", -1))
        root.bind("p", lambda e: self.update_score("b", 1))
        root.bind("l", lambda e: self.update_score("b", -1))
        root.bind("r", lambda e: self.reset_scores())
        root.bind("i", lambda e: self.toggle_replay())
        root.bind("<Escape>", lambda e: root.destroy())

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

        if self.showing_replay:
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
        self.showing_replay = False
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