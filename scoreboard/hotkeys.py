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


def _chord_case_variants(parsed: str) -> list[str]:
    """Windows/Tk sometimes deliver a different letter case; bind both."""
    if not (parsed.startswith("<") and parsed.endswith(">")):
        return [parsed]
    inner = parsed[1:-1]
    parts = inner.split("-")
    if not parts:
        return [parsed]
    last = parts[-1]
    if len(last) != 1 or not last.isalpha():
        return [parsed]
    alt = last.swapcase()
    alt_inner = "-".join(parts[:-1] + [alt])
    return [parsed, f"<{alt_inner}>"]


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
                widget.bind_all(char, handler)
                other = char.swapcase()
                if other != char:
                    widget.bind_all(other, handler)
            else:
                widget.bind_all(char, handler)
            _LOG.debug("Bound hotkey (legacy, bind_all) %r", candidate)
            return
        widget.bind_all(parsed, handler)
        _LOG.debug("Bound hotkey (bind_all) %r -> %s", candidate, parsed)
        return
    _LOG.error("Failed to bind hotkey; spec=%r default=%r", spec, default_spec)


def bind_recording_hotkey_global(
    root: tk.Misc,
    spec: str | None,
    default_spec: str,
    handler: Callable[[tk.Event], None],
) -> None:
    """
    Bind chord hotkeys with bind_all so they fire even when a Toplevel (e.g. recording
    overlay) has focus. Legacy single-key specs fall back to widget-only bind.
    """
    for candidate in (spec, default_spec):
        if not candidate:
            continue
        parsed = parse_recording_hotkey_to_tk_bind(candidate)
        if parsed is None:
            _LOG.debug("Could not parse hotkey %r; trying next candidate", candidate)
            continue
        if isinstance(parsed, tuple) and parsed[0] == "legacy":
            bind_recording_hotkey(root, spec, default_spec, handler)
            return
        for seq in _chord_case_variants(parsed):
            root.bind_all(seq, handler)
        _LOG.debug("bind_all hotkey %r -> %s", candidate, parsed)
        return
    _LOG.error("Failed to bind_global hotkey; spec=%r default=%r", spec, default_spec)
