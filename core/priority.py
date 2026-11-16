"""Shared task/priority ordering used by BOTH the rate limiter and scheduler.

Lives in its own tiny module so neither component imports the other just for
this enum (avoids a circular dependency while keeping one source of truth).
"""

from __future__ import annotations

from enum import IntEnum


class Priority(IntEnum):
    """Queue priority: HIGH actions are granted before NORMAL before LOW."""

    HIGH = 0
    NORMAL = 1
    LOW = 2

    @classmethod
    def coerce(cls, value: int | str | Priority | None) -> Priority:
        """Best-effort conversion (int value, name string, or None -> NORMAL).

        Raises ValueError for anything unrecognized — callers surface it as a
        usage error rather than silently guessing.
        """
        if value is None:
            return cls.NORMAL
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise ValueError(f"priority must not be a boolean, got {value!r}")
        if isinstance(value, int):
            return cls(value)
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError as exc:
                raise ValueError(
                    f"unknown priority {value!r} (use one of {[p.name for p in cls]})"
                ) from exc
        raise ValueError(f"cannot interpret priority {value!r}")


__all__ = ["Priority"]
