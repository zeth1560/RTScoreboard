"""Central logging setup for the scoreboard application."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Rotating log file: size and number of backup files (scoreboard.log.1, .2, …).
_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 5


def configure_logging(
    level: int = logging.INFO,
    *,
    log_file: str | None = None,
) -> None:
    """Configure root logger: stderr + optional rotating file; idempotent for first setup."""
    root = logging.getLogger()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not root.handlers:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        root.addHandler(stderr_handler)

        if log_file:
            try:
                path = Path(log_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    path,
                    maxBytes=_LOG_FILE_MAX_BYTES,
                    backupCount=_LOG_FILE_BACKUP_COUNT,
                    encoding="utf-8",
                )
                file_handler.setFormatter(formatter)
                root.addHandler(file_handler)
            except OSError:
                print(
                    f"scoreboard: could not open log file {log_file!r} (using stderr only)",
                    file=sys.stderr,
                )

    root.setLevel(level)
    for h in root.handlers:
        h.setLevel(level)
