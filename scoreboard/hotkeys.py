"""Recording / UI hotkey parsing and Tk binding helpers."""

from __future__ import annotations

import logging
import re
import tkinter as tk
from typing import Callable

_LOG = logging.getLogger(__name__)


def parse_recording_hotkey_to_tk_bind(spec: str | None):
    """
    Map .env-style chords to Tk bind() sequences, e.g. Ctrl+Shift+g -> <Control-Shift-G>.
    Plain single letter (no '+') uses legacy tuple ("legacy", "g") for case-insensitive bind.
    Returns None if invalid.
    """
    raw = (spec or "").strip()
    if not raw:
        return None

    if "+" not in raw:
        key = raw[:1]
        if len(key) == 1 and (key.isalpha() or key.isdigit()):
            return ("legacy", key.lower() if key.isalpha() else key)
        return None

    parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    if len(parts) < 2:
        return None

    mod_map = {
        "ctrl": "Control",
        "control": "Control",
        "alt": "Alt",
        "shift": "Shift",
        "meta": "Meta",
        "win": "Meta",
        "cmd": "Meta",
    }
    mod_order = {"Control": 0, "Alt": 1, "Shift": 2, "Meta": 3}

    modifiers = []
    for p in parts[:-1]:
        m = mod_map.get(p)
        if m is None:
            return None
        if m not in modifiers:
            modifiers.append(m)

    key_raw = parts[-1]
    if mod_map.get(key_raw) is not None:
        return None

    key = None
    if len(key_raw) == 1:
        if key_raw.isalpha():
            key = key_raw.upper() if "Shift" in modifiers else key_raw.lower()
        elif key_raw.isdigit():
            key = key_raw
        else:
            return None
    elif re.fullmatch(r"f([1-9]|1[0-2])", key_raw):
        key = "F" + str(int(key_raw[1:]))
    else:
        return None

    modifiers.sort(key=lambda m: mod_order.get(m, 99))
    inner = "-".join(modifiers + [key])
    return f"<{inner}>"


def bind_recording_hotkey(
    widget: tk.Misc,
    spec: str | None,
    default_spec: str,
    handler: Callable[[tk.Event], None],
) -> None:
    """Bind a recording hotkey from env, or default chord if parsing fails."""
    for candidate in (spec, default_spec):
        if not candidate:
            continue
        parsed = parse_recording_hotkey_to_tk_bind(candidate)
        if parsed is None:
            _LOG.debug("Could not parse hotkey %r; trying next candidate", candidate)
            continue
        if isinstance(parsed, tuple) and parsed[0] == "legacy":
            char = parsed[1]
            if len(char) == 1 and char.isalpha():
                widget.bind(char, handler)
                other = char.swapcase()
                if other != char:
                    widget.bind(other, handler)
            else:
                widget.bind(char, handler)
            _LOG.debug("Bound hotkey (legacy) %r", candidate)
            return
        widget.bind(parsed, handler)
        _LOG.debug("Bound hotkey %r -> %s", candidate, parsed)
        return
    _LOG.error("Failed to bind hotkey; spec=%r default=%r", spec, default_spec)
