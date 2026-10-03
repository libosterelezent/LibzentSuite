"""Pure text transforms for the soulcopy plugin (no I/O -> fully unit-testable).

Semantics ported verbatim from the audited original (Soulcopy/autocopy.py):
  * each Mudae DM page: if the first line contains 'Soulmates', drop the first
    two lines (header + separator),
  * all remaining lines are collapsed with '$' as separator (Mudae's list mode),
  * the collapsed stream is re-split into chunks that each fit
    len(prefix) + content <= max_chunk_length,
  * improvement over the original: a single entry longer than the limit is
    hard-split instead of silently producing an oversized chunk.
"""
from __future__ import annotations

from typing import Iterable, List, Sequence

DEFAULT_PREFIX = "$soulcopy "
HEADER_MARKER = "Soulmates"


def strip_page_header(page: str) -> List[str]:
    """One DM page -> its lines, minus the 'Soulmates' header block when present."""
    lines = (page or "").split("\n")
    if lines and HEADER_MARKER in lines[0]:
        lines = lines[2:]
    return [line for line in lines if line.strip()]


def collapse_pages(pages: Iterable[str]) -> str:
    """All pages -> one '$'-joined stream (Mudae list encoding).

    Returns '' when every page is empty/header-only.
    """
    entries: List[str] = []
    for page in pages:
        entries.extend(strip_page_header(page))
    return "$".join(entries)


def _u16len(s: str) -> int:
    """Length in UTF-16 code units — Discord's message-size unit.

    Astral-plane characters (emoji — common in Mudae names) count 2, so a
    codepoint-based budget can build chunks the API rejects with 400.
    """
    return len(s.encode("utf-16-le")) // 2


def split_into_chunks(stream: str, max_chunk_length: int, prefix: str) -> List[str]:
    """'$'-separated stream -> chunks fitting prefix + content in max_chunk_length.

    The budget is measured in UTF-16 code units (Discord's limit). Raises
    ValueError when the budget is too small for prefix + one character.
    """
    if max_chunk_length < _u16len(prefix) + 1:
        # raise only when prefix + ONE character cannot fit
        raise ValueError(
            f"max_chunk_length {max_chunk_length} too small for prefix {prefix!r}"
        )
    budget = max_chunk_length - _u16len(prefix)
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for entry in stream.split("$"):
        piece = entry
        while len(piece) > 0:
            needed = _u16len(piece) + (1 if current else 0)
            if current and current_len + needed > budget:
                chunks.append("$".join(current))
                current, current_len = [], 0
                needed = _u16len(piece)
            if needed > budget:  # single oversized entry: hard-split
                # walk back to a codepoint cut whose u16 length fits the
                # budget (never split inside a surrogate pair)
                cut = len(piece)
                while cut > 0 and _u16len(piece[:cut]) > budget:
                    cut -= 1
                if cut <= 0:  # prefix dominates the budget entirely
                    raise ValueError(
                        f"max_chunk_length {max_chunk_length} too small "
                        f"for prefix {prefix!r}"
                    )
                chunks.append(piece[:cut])
                piece = piece[cut:]
                continue
            if current:
                current_len += 1  # joining '$'
            current.append(piece)
            current_len += _u16len(piece)
            piece = ""
    if current:
        chunks.append("$".join(current))
    return chunks


def build_soulcopy_chunks(
    pages: Sequence[str],
    max_chunk_length: int = 2000,
    prefix: str = DEFAULT_PREFIX,
) -> List[str]:
    """Full pipeline: DM pages -> ready-to-send '$soulcopy ...' chunks."""
    stream = collapse_pages(pages)
    if not stream:
        return []
    return [prefix + chunk for chunk in split_into_chunks(stream, max_chunk_length, prefix)]


__all__ = [
    "DEFAULT_PREFIX", "HEADER_MARKER", "strip_page_header", "collapse_pages",
    "split_into_chunks", "build_soulcopy_chunks",
]
