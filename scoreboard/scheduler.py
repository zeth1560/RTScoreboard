"""Centralized Tk ``after`` scheduling with safe cancel, optional job names, and debug logs."""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_BG_RESILIENCE_MAX = 5


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
        self._resilience_failures: dict[str, int] = {}
        self._resilience_disabled: set[str] = set()

    def schedule(
        self,
        delay_ms: int,
        callback: Callable[[], Any],
        *,
        name: str | None = None,
        background_resilience: bool = False,
        max_consecutive_failures: int = _DEFAULT_BG_RESILIENCE_MAX,
    ) -> str | None:
        """Schedule callback; exceptions in callback are logged. Returns job id or None.

        When ``background_resilience`` is True and ``name`` is set, repeated uncaught
        exceptions (excluding TclError) increment a per-name counter; after
        ``max_consecutive_failures`` the job name is disabled and further schedules
        with that name are skipped. Successful runs reset the counter. Hotkey paths
        do not use this; only recurring background work should.
        """
        label = name or ""

        if background_resilience:
            if not label:
                _LOG.debug("background_resilience ignored (no job name)")
                background_resilience = False
            elif label in self._resilience_disabled:
                self._log.debug(
                    "Scheduler: skip schedule (disabled after failures) name=%r",
                    label,
                )
                return None

        def wrapper() -> None:
            self._jobs.discard(jid)
            self._job_names.pop(jid, None)
            if self._debug_schedule and label:
                self._log.debug("after fired name=%r", label)
            if background_resilience and label in self._resilience_disabled:
                return
            try:
                callback()
                if background_resilience and label:
                    self._resilience_failures.pop(label, None)
            except tk.TclError:
                self._log.debug(
                    "after callback TclError name=%r (widget destroyed?)",
                    label,
                    exc_info=True,
                )
            except Exception:
                self._log.exception("after callback failed name=%r", label)
                if background_resilience and label:
                    n = self._resilience_failures.get(label, 0) + 1
                    self._resilience_failures[label] = n
                    if n >= max_consecutive_failures:
                        self._resilience_disabled.add(label)
                        self._log.error(
                            "Scheduler: disabled background job %r after %s consecutive failures",
                            label,
                            n,
                        )

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
        **kwargs: Any,
    ) -> str | None:
        jid = self._scheduler.schedule(delay_ms, callback, name=name, **kwargs)
        self._ids.append(jid)
        return jid

    def cancel_all(self) -> None:
        for jid in self._ids:
            self._scheduler.cancel(jid)
        self._ids.clear()
