"""Centralized Tk ``after`` scheduling with safe cancel, optional job names, and debug logs."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from typing import Any

_LOG = logging.getLogger(__name__)


class AfterScheduler:
    """Tracks scheduled callbacks so shutdown and teardown can cancel reliably."""

    def __init__(
        self,
        root: tk.Misc,
        logger: logging.Logger | None = None,
        *,
        debug_schedule: bool = False,
    ) -> None:
        self._root = root
        self._log = logger or _LOG
        self._debug_schedule = debug_schedule
        self._jobs: set[str] = set()
        self._job_names: dict[str, str] = {}

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], Any],
        *,
        name: str | None = None,
    ) -> str | None:
        """Schedule callback; exceptions in callback are logged. Returns job id or None."""

        def wrapper() -> None:
            self._jobs.discard(jid)
            self._job_names.pop(jid, None)
            if self._debug_schedule and label:
                self._log.debug("after fired name=%r", label)
            try:
                callback()
            except tk.TclError:
                self._log.debug(
                    "after callback TclError name=%r (widget destroyed?)",
                    label,
                    exc_info=True,
                )
            except Exception:
                self._log.exception("after callback failed name=%r", label)

        label = name or ""
        jid = self._root.after(delay_ms, wrapper)
        self._jobs.add(jid)
        if name:
            self._job_names[jid] = name
        if self._debug_schedule:
            self._log.debug("after schedule name=%r delay_ms=%s id=%s", name, delay_ms, jid)
        return jid

    def cancel(self, job_id: str | None) -> None:
        if job_id is None:
            return
        label = self._job_names.pop(job_id, "")
        try:
            self._root.after_cancel(job_id)
        except (ValueError, tk.TclError) as e:
            self._log.debug("after_cancel ignored id=%s name=%r: %s", job_id, label, e)
        self._jobs.discard(job_id)
        if self._debug_schedule and label:
            self._log.debug("after cancel name=%r id=%s", label, job_id)

    def cancel_all_tracked(self) -> None:
        for jid in list(self._jobs):
            self.cancel(jid)


class JobGroup:
    """Bundle several after() ids for feature teardown (e.g. screensaver fade + interval)."""

    def __init__(self, scheduler: AfterScheduler) -> None:
        self._scheduler = scheduler
        self._ids: list[str | None] = []

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], Any],
        *,
        name: str | None = None,
    ) -> str | None:
        jid = self._scheduler.schedule(delay_ms, callback, name=name)
        self._ids.append(jid)
        return jid

    def cancel_all(self) -> None:
        for jid in self._ids:
            self._scheduler.cancel(jid)
        self._ids.clear()
