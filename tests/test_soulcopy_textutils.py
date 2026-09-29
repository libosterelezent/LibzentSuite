"""Soulcopy text pipeline: header stripping, collapse, chunk splitting."""
from __future__ import annotations

import unittest

from plugins.soulcopy import textutils


class TestPageHandling(unittest.TestCase):
    def test_header_stripped_when_marker_present(self) -> None:
        page = "Soulmates\n---------\nAlice\nBob"
        self.assertEqual(textutils.strip_page_header(page), ["Alice", "Bob"])

    def test_no_header_kept_intact(self) -> None:
        page = "Dana\nEve"
        self.assertEqual(textutils.strip_page_header(page), ["Dana", "Eve"])

    def test_collapse_pages(self) -> None:
        pages = ["Soulmates\n---------\nAlice\nBob", "Charlie"]
        self.assertEqual(textutils.collapse_pages(pages), "Alice$Bob$Charlie")

    def test_collapse_empty_pages(self) -> None:
        self.assertEqual(textutils.collapse_pages(["Soulmates\n---------", ""]), "")


class TestChunking(unittest.TestCase):
    def test_chunks_respect_budget(self) -> None:
        chunks = textutils.build_soulcopy_chunks(
            ["Soulmates\n---------\nAlice\nBob", "Charlie"], max_chunk_length=13
        )
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 13)
            self.assertTrue(chunk.startswith("$soulcopy "))

    def test_exact_expected_splits(self) -> None:
        chunks = textutils.build_soulcopy_chunks(
            ["Soulmates\n---------\nAlice\nBob", "Charlie"], max_chunk_length=13
        )
        self.assertEqual(
            chunks,
            [
                "$soulcopy Ali",
                "$soulcopy ce",
                "$soulcopy Bob",
                "$soulcopy Cha",
                "$soulcopy rli",
                "$soulcopy e",
            ],
        )

    def test_round_trip_reassembles_stream(self) -> None:
        pages = ["Soulmates\n---------\nAlice\nBob", "Charlie"]
        chunks = textutils.build_soulcopy_chunks(pages, max_chunk_length=60)
        stream = textutils.collapse_pages(pages)
        rejoined = "$".join(chunk[len("$soulcopy "):] for chunk in chunks)
        self.assertEqual(rejoined, stream)

    def test_single_oversized_entry_hard_split(self) -> None:
        stream = "a" * 30
        chunks = textutils.split_into_chunks(stream, max_chunk_length=10, prefix="")  # budget 10
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 10)
        self.assertEqual("".join(chunks), stream)

    def test_prefix_too_long_raises(self) -> None:
        with self.assertRaises(ValueError):
            textutils.split_into_chunks("x", max_chunk_length=5, prefix="$soulcopy ")

    def test_empty_input_yields_no_chunks(self) -> None:
        self.assertEqual(textutils.build_soulcopy_chunks([]), [])
        self.assertEqual(textutils.build_soulcopy_chunks(["Soulmates\n---", ""]), [])


if __name__ == "__main__":
    unittest.main()
