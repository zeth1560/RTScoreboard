"""Write scoreboard state for external launcher consumption (JSON file)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_launcher_status_json(path: str | Path, payload: dict[str, Any]) -> bool:
    """Atomically write JSON so readers never see a partial file. Returns True if the file was replaced."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        _LOG.warning("Launcher status: could not create directory %s", p.parent, exc_info=True)
        return False

    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd: int | None = None
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            suffix=".json.tmp",
            prefix="scoreboard_status_",
            dir=str(p.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None
            f.write(body)
        os.replace(tmp_name, p)
        tmp_name = None
        return True
    except OSError:
        _LOG.warning("Launcher status: write failed for %s", p, exc_info=True)
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name and os.path.isfile(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
