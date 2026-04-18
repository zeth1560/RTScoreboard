"""Infer long-recording (capture) state from encoder_state.json for the recording overlay."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from scoreboard.encoder_status_overlay import _is_payload_stale

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class EncoderRecordingSnapshot:
    """Result of reading ``encoder_state.json`` for recording-overlay sync."""

    usable: bool
    """False if file missing, invalid JSON, or ``updated_at`` is stale."""

    capturing: bool
    """When ``usable``, whether the encoder indicates an active capture session."""

    session_seq: int | None
    """``long_recording_session_seq`` from the payload (for caller bookkeeping)."""


def _parse_session_seq(data: dict) -> int | None:
    raw = data.get("long_recording_session_seq")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def infer_capturing_from_payload(
    data: dict,
    prev_seq: int | None,
) -> tuple[bool, int | None]:
    """Apply operator rules: start/stop signals from encoder JSON.

    Started: ``long_recording_active`` is True, or ``state == "recording"``, or
    ``long_recording_session_seq`` increased vs ``prev_seq``.

    Stopped: ``long_recording_active`` is False and
    (``long_recording_started_at`` is null/empty OR ``state != "recording"``).
    """
    seq = _parse_session_seq(data)
    state = str(data.get("state", "")).strip().lower()
    lra = data.get("long_recording_active")
    sat = data.get("long_recording_started_at")

    def sat_null() -> bool:
        if sat is None:
            return True
        t = str(sat).strip()
        return t == "" or t.lower() == "null"

    seq_edge = prev_seq is not None and seq is not None and seq > prev_seq

    if lra is True:
        return True, seq
    if state == "recording":
        return True, seq
    if seq_edge:
        return True, seq

    if lra is False and (sat_null() or state != "recording"):
        return False, seq

    if state == "recording":
        return True, seq

    return False, seq


def load_encoder_recording_snapshot(
    path: Path,
    stale_seconds: int,
    prev_seq: int | None,
) -> EncoderRecordingSnapshot:
    """Read JSON from disk; return :class:`EncoderRecordingSnapshot`."""
    if not path.is_file():
        return EncoderRecordingSnapshot(usable=False, capturing=False, session_seq=prev_seq)

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        _LOG.debug("encoder recording sync: read/parse failed for %s", path, exc_info=True)
        return EncoderRecordingSnapshot(usable=False, capturing=False, session_seq=prev_seq)

    if not isinstance(data, dict):
        return EncoderRecordingSnapshot(usable=False, capturing=False, session_seq=prev_seq)

    if _is_payload_stale(data.get("updated_at"), stale_seconds):
        return EncoderRecordingSnapshot(usable=False, capturing=False, session_seq=prev_seq)

    capturing, seq = infer_capturing_from_payload(data, prev_seq)
    return EncoderRecordingSnapshot(usable=True, capturing=capturing, session_seq=seq)
