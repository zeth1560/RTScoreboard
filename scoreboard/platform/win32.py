"""Windows-specific foreground and synthetic input helpers."""

from __future__ import annotations

import logging
import os

_LOG = logging.getLogger(__name__)

if os.name == "nt":
    import ctypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    class _WinRECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def win32_synthetic_click_window_center(hwnd_int: int) -> None:
        user32 = ctypes.windll.user32
        rect = _WinRECT()
        if not user32.GetWindowRect(hwnd_int, ctypes.byref(rect)):
            _LOG.debug("GetWindowRect failed for hwnd=%s", hwnd_int)
            return

        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        user32.SetCursorPos(cx, cy)
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def win32_force_foreground(hwnd_int: int) -> None:
        hwnd = hwnd_int
        user32 = _user32
        kernel32 = _kernel32

        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SW_RESTORE = 9

        foreground = user32.GetForegroundWindow()
        current_tid = kernel32.GetCurrentThreadId()
        fg_tid = None

        if foreground:
            pid_dummy = ctypes.c_ulong()
            fg_tid = user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid_dummy))

        if fg_tid and fg_tid != current_tid:
            user32.AttachThreadInput(fg_tid, current_tid, True)

        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE,
            )
            user32.SetWindowPos(
                hwnd,
                HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE,
            )
            user32.SetForegroundWindow(hwnd)
        except Exception:
            _LOG.debug(
                "win32_force_foreground failed (hwnd=%s); see SCOREBOARD_DEBUG=1 for noisy runs",
                hwnd_int,
                exc_info=True,
            )
        finally:
            if fg_tid and fg_tid != current_tid:
                user32.AttachThreadInput(fg_tid, current_tid, False)

else:

    def win32_synthetic_click_window_center(hwnd_int: int) -> None:
        return

    def win32_force_foreground(hwnd_int: int) -> None:
        return
