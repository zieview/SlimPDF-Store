"""Logging initialization for source and packaged execution."""

import logging
from pathlib import Path

from slimpdf.config import LOG_FORMAT, get_log_file


def configure_logging(base_directory: Path) -> Path:
    """Configure the root logger and return the selected log path."""

    log_path = get_log_file(base_directory)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return log_path
