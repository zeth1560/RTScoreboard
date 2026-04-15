"""Optional OBS Studio WebSocket probe before starting the recording overlay (opt-in)."""

from __future__ import annotations

import logging

from scoreboard.config.settings import Settings

_LOG = logging.getLogger(__name__)


def _obs_websocket_recording_gate_result(
    settings: Settings,
    *,
    log_connection_failures: bool = True,
) -> tuple[bool, str]:
    """
    WebSocket probe only (no RECORDING_OBS_HEALTH_CHECK flag). Run from a thread.

    Returns (True, "") when OBS answers and optional main-output rule passes.
    """
    try:
        import obsws_python as obs
        from obsws_python.error import OBSSDKError, OBSSDKTimeoutError
    except ImportError:
        return (
            False,
            "obsws-python is not installed (pip install obsws-python).",
        )

    timeout = settings.obs_websocket_timeout_sec
    try:
        with obs.ReqClient(
            host=settings.obs_websocket_host,
            port=settings.obs_websocket_port,
            password=settings.obs_websocket_password or "",
            timeout=timeout,
        ) as client:
            client.get_version()
            status = client.get_record_status()
    except OBSSDKTimeoutError:
        if log_connection_failures:
            _LOG.warning(
                "OBS WebSocket timed out after %.1fs — OBS may be hung or the server is off",
                timeout,
            )
        return (
            False,
            "OBS did not answer in time — it may be stuck closing a recording or not "
            "responding. Check OBS, then try again.",
        )
    except OBSSDKError as e:
        if log_connection_failures:
            _LOG.warning("OBS WebSocket request failed: %s", e)
        return (
            False,
            f"OBS WebSocket error ({e!s}). Is OBS running with Tools → WebSocket Server enabled?",
        )
    except OSError as e:
        if log_connection_failures:
            _LOG.warning("OBS WebSocket connection failed: %s", e)
        return (
            False,
            "Could not connect to OBS WebSocket (localhost). Is OBS running?",
        )

    output_active = getattr(status, "output_active", None)
    if output_active is None:
        output_active = getattr(status, "outputActive", False)

    if settings.recording_obs_block_if_main_recording and output_active:
        if log_connection_failures:
            _LOG.info(
                "Recording start blocked: OBS main recording output is still active "
                "(stop or finish in OBS, or set RECORDING_OBS_BLOCK_IF_MAIN_RECORDING=0)",
            )
        return (
            False,
            "OBS still shows the main recorder as active — wait for it to finish or "
            "stop it in OBS before starting the session timer.",
        )

    return (True, "")


def probe_obs_video_recorder_ready(settings: Settings) -> bool:
    """True if OBS WebSocket is reachable and the same rules as the recording gate pass."""
    require_idle = settings.obs_status_require_main_output_idle
    original_block = settings.recording_obs_block_if_main_recording
    effective_settings = settings
    if not require_idle and original_block:
        # Status strip should represent OBS availability, not busy/idle, by default.
        effective_settings = Settings(
            **{
                **settings.__dict__,
                "recording_obs_block_if_main_recording": False,
            }
        )
    ok, _reason = _obs_websocket_recording_gate_result(
        effective_settings,
        log_connection_failures=False,
    )
    return ok


def check_obs_recording_gate(settings: Settings) -> tuple[bool, str]:
    """
    Blocking health check — run from a background thread only.

    Uses OBS WebSocket 5.x (built into OBS 28+). Verifies OBS answers promptly and,
    optionally, that the main recording output is not still active (common when OBS is
    finalizing a file or stuck).

    Returns:
        (True, "") if recording overlay may start.
        (False, reason) if the operator should fix OBS first.

    Note:
        Source Record / replay buffer do not always toggle the *main* record output.
        If you only use those, set RECORDING_OBS_BLOCK_IF_MAIN_RECORDING=0.
    """
    if not settings.recording_obs_health_check:
        return (True, "")

    ok, reason = _obs_websocket_recording_gate_result(
        settings,
        log_connection_failures=True,
    )
    if ok:
        return (True, "")
    if "obsws-python is not installed" in reason:
        _LOG.error(
            "RECORDING_OBS_HEALTH_CHECK is on but obsws-python is not installed "
            "(pip install obsws-python)",
        )
        return (
            False,
            "Recording guard is on but obsws-python is missing. Install it or turn "
            "RECORDING_OBS_HEALTH_CHECK off.",
        )
    return (False, reason)
