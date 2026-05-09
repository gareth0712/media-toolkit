"""Unit tests for ``media_toolkit.logging_setup``."""

from __future__ import annotations

import logging
from pathlib import Path

from media_toolkit.logging_setup import LOGGER_NAME, configure_logging


def test_configure_logging_idempotent(tmp_path: Path) -> None:
    """Calling configure_logging twice should not duplicate handlers."""
    log_file = tmp_path / "media-toolkit.log"

    configure_logging(log_file)
    first_count = len(logging.getLogger(LOGGER_NAME).handlers)

    configure_logging(log_file)
    second_count = len(logging.getLogger(LOGGER_NAME).handlers)

    # Exactly one file handler + one console handler each call.
    assert first_count == 2
    assert second_count == 2

    file_handlers = [
        h
        for h in logging.getLogger(LOGGER_NAME).handlers
        if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1


def test_configure_logging_creates_parent_dir(tmp_path: Path) -> None:
    """The parent dir of the log file is created if it doesn't already exist."""
    nested = tmp_path / "a" / "b" / "c"
    log_file = nested / "media-toolkit.log"
    assert not nested.exists()

    configure_logging(log_file)

    assert nested.exists()
    assert log_file.exists()
