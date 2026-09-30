"""Pure text transforms for the soulcopy plugin (no I/O -> fully unit-testable).

Semantics ported from the original Soulcopy/autocopy.py:
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
    lines = page.split("\n")
    if lines and HEADER_MARKER in lines[0]:
        lines = lines[2:]
    return [line for line in lines if line != ""]


def collapse_pages(pages: Iterable[str]) -> str:
    """All pages -> one '$'-joined stream (Mudae list encoding).

    Returns '' when every page is empty/header-only.
    """
    entries: List[str] = []
    for page in pages:
        entries.extend(strip_page_header(page))
    return "$".join(entries)


def split_into_chunks(stream: str, max_chunk_length: int, prefix: str) -> List[str]:
    """'$'-separated stream -> chunks fitting prefix + content in max_chunk_length.

    Raises ValueError when the budget is too small for prefix + one character.
    """
    if max_chunk_length < len(prefix) + 1:
        # AUDIT FIX: raise only when prefix + ONE character cannot fit
        raise ValueError(
            f"max_chunk_length {max_chunk_length} too small for prefix {prefix!r}"
        )
    budget = max_chunk_length - len(prefix)
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for entry in stream.split("$"):
        piece = entry
        while len(piece) > 0:
            needed = len(piece) + (1 if current else 0)
            if current and current_len + needed > budget:
                chunks.append("$".join(current))
                current, current_len = [], 0
                needed = len(piece)
            if needed > budget:  # single oversized entry: hard-split
                cut = budget
                chunks.append(piece[:cut])
                piece = piece[cut:]
                continue
            if current:
                current_len += 1  # joining '$'
            current.append(piece)
            current_len += len(piece)
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
