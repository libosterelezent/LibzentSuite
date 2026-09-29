"""Logging setup: one namespaced logger tree ("mudae.*")."""
from __future__ import annotations

import logging
import sys

ROOT_LOGGER = "mudae"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO, stream=None) -> logging.Logger:
    """Configure the 'mudae' logger tree; idempotent.

    Expects: optional stdlib level and output stream (default stderr).
    Returns: the root 'mudae' logger with a single stream handler attached.
    """
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def plugin_logger(account: str, plugin: str) -> logging.Logger:
    """Logger for a plugin running on a specific account: mudae.<account>.<plugin>."""
    return logging.getLogger(f"{ROOT_LOGGER}.{account}.{plugin}")
