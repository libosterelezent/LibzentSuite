"""Logging — component facade (see core/components.py)."""
from .logging_setup import (  # noqa: F401
    RingBufferHandler,
    attach_ring_buffer,
    plugin_logger,
    setup_logging,
)
