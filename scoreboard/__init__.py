"""Scoreboard application package (refactored from monolithic main)."""

__all__ = ["configure_logging", "load_settings"]

from scoreboard.logging_config import configure_logging
from scoreboard.config.settings import load_settings
