"""Score persistence: UTF-8 JSON with atomic replace."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)


@dataclass
class ScoreState:
    score_a: int = 0
    score_b: int = 0


def load_scores(
    path: str | Path,
    *,
    rewrite_defaults_if_corrupt: bool = True,
) -> ScoreState:
    p = Path(path)
    if not p.is_file():
        _LOG.info("State file %s does not exist; starting at 0-0", p)
        return ScoreState()
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        a = int(data.get("score_a", 0))
        b = int(data.get("score_b", 0))
        _LOG.info("Loaded scores from %s: %s-%s", p, a, b)
        return ScoreState(score_a=a, score_b=b)
    except OSError:
        _LOG.exception("Failed to read state file %s; using defaults", p)
        return ScoreState()
    except (json.JSONDecodeError, TypeError, ValueError):
        _LOG.exception(
            "Invalid or corrupt state JSON in %s; resetting to 0-0 and rewriting file",
            p,
        )
        fresh = ScoreState()
        if rewrite_defaults_if_corrupt:
            try:
                save_scores(p, fresh)
                _LOG.warning("Rewrote corrupt state file with default scores at %s", p)
            except OSError:
                _LOG.exception("Could not rewrite corrupt state file %s", p)
        return fresh


def save_scores(path: str | Path, state: ScoreState) -> None:
    p = Path(path)
    payload = {"score_a": state.score_a, "score_b": state.score_b}
    directory = p.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(directory),
        prefix=".state_",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, p)
        _LOG.debug("Saved scores to %s: %s-%s", p, state.score_a, state.score_b)
    except OSError:
        _LOG.exception("Failed to save state atomically to %s", p)
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            _LOG.warning("Could not remove temp state file %s", tmp_path, exc_info=True)
