"""Theme store — the dashboard theme engine's durable state.

The theme engine (interfaces/dashboard/static/js/theme.js) keeps its state
in localStorage for instant paint, and mirrors it here so fonts, accent,
custom CSS and presets survive a browser change. This is a DASHBOARD
PRESENTATION concern: it deliberately does NOT go through ControlAPI (the
CLI has no theme), the dashboard server owns the two routes that read and
write it.

Shape (owned by the frontend engine; this module only validates the
envelope, never the semantics):

    {
      "scope": "global",
      "global": {...},
      "pages": {...},
      "pageCss": {...},
      "updated_at": 1730000000000
    }

Atomic write (tmp + os.replace) so a crash mid-save can never leave a
truncated file behind.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: module-level so tests can point it at a tmp file
PATH = Path("state/theme.json")

#: generous cap — the state is a few KB of tokens plus user CSS
MAX_BYTES = 256 * 1024


def load() -> dict:
    """The persisted theme state, or {} when absent/corrupt (a broken
    theme file must never take the dashboard API down)."""
    try:
        raw = PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        state = json.loads(raw)
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def save(state: dict) -> None:
    """Persist the theme state atomically. Raises ValueError on an
    unusable payload (not an object, or over the size cap)."""
    if not isinstance(state, dict):
        raise ValueError("theme state must be a JSON object")
    try:
        raw = json.dumps(state, ensure_ascii=False, indent=1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"theme state is not JSON-serializable: {exc}") from exc
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"theme state too large (>{MAX_BYTES} bytes) — "
                         "trim the custom CSS")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(raw, encoding="utf-8")
    os.replace(tmp, PATH)
