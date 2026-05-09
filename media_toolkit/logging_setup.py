"""Centralised logging configuration for media-toolkit.

Configures the ``"media_toolkit"`` root logger with a file handler (always
DEBUG level) and a console handler (caller-controlled level). All submodules
should obtain their logger via ``logging.getLogger(__name__)`` so messages
are routed through this configuration automatically.
"""

import logging
import sys
from pathlib import Path

LOGGER_NAME = "media_toolkit"
FILE_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_LOG_FORMAT = "%(message)s"


def configure_logging(log_file: Path, console_level: int = logging.INFO) -> None:
    """Configure the ``media_toolkit`` logger with a file and console handler.

    Idempotent: existing handlers on the ``media_toolkit`` logger are removed
    before new ones are attached. The parent directory of ``log_file`` is
    created if it does not already exist.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    # Prevent duplicate emission via the root logger.
    logger.propagate = False

    # Drop any pre-existing handlers so calling this twice is safe.
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # noqa: BLE001 - close failures must not crash CLI
            pass

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_LOG_FORMAT))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(CONSOLE_LOG_FORMAT))
    logger.addHandler(console_handler)
