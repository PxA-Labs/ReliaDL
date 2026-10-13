"""
Tests for the scheduling seam between the engine and the algorithm modules.

The concurrency floor is the piece worth pinning hardest: without it the BL-DCS
controller grows chunks until fewer remain than there are workers, and measured
throughput roughly halves. These tests fix both the invariant it must maintain
(the partition stays contiguous and complete) and the property it exists for
(enough chunks remain to occupy the pool).
"""

from __future__ import annotations

import unittest

from reliadl.algorithms.adaptive_chunker import MIN_CHUNK_SIZE, DynamicChunkPlanner
from reliadl.exceptions import ConfigurationError
from reliadl.scheduling import TransferScheduler, derive_mirror_id

MB = 1024 * 1024


def _drain(scheduler: TransferScheduler) -> list:
    """Plan the whole file, feeding back a fast clean transfer after each batch."""
    chunks: list = []
    while True:
        batch = scheduler.plan_more(8)
        if not batch:
            return chunks
        chunks += batch
        # A clean, fast path is what drives the controller towards its ceiling,
        # which is exactly where the floor has to bite.
        scheduler.record_success(
            None,
            bytes_transferred=batch[-1].size,
            duration_seconds=0.02,
            rtt_seconds=0.005,
        )


class InertSchedulerTest(unittest.TestCase):
    """With no policy enabled the seam must reproduce the old behaviour exactly."""

    def test_fixed_partition_matches_plan_chunks(self) -> None:
        from reliadl.download_engine import plan_chunks

        scheduler = TransferScheduler(
            origin_url="http://origin/f", file_size=10 * MB + 1, fixed_chunk_size=4 * MB
        )
        planned = scheduler.plan_initial()
        expected = plan_chunks(10 * MB + 1, 4 * MB)
        self.assertEqual(
            [(c.start_byte, c.end_byte) for c in planned],
            [(c.start_byte, c.end_byte) for c in expected],
        )

    def test_select_mirror_returns_the_origin(self) -> None:
        scheduler = TransferScheduler(
            origin_url="http://origin/f", file_size=MB, fixed_chunk_size=MB
        )
        selection = scheduler.select_mirror()
        self.assertIsNotNone(selection)
        self.assertEqual(selection.url, "http://origin/f")
        self.assertIsNone(selection.request)


class ConcurrencyFloorTest(unittest.TestCase):
    def test_partition_is_contiguous_and_complete(self) -> None:
        size = 64 * MB
        scheduler = TransferScheduler(
            origin_url="http://origin/f",
            file_size=size,
            fixed_chunk_size=4 * MB,
            adaptive_chunk_sizing=True,
            workers=4,
        )
        chunks = _drain(scheduler)

        self.assertEqual(sum(c.size for c in chunks), size)
        self.assertEqual(chunks[0].start_byte, 0)
        self.assertEqual(chunks[-1].end_byte, size - 1)
        # Pairwise, so the second sequence is shorter by one by construction.
        for earlier, later in zip(chunks, chunks[1:], strict=False):
            self.assertEqual(earlier.end_byte + 1, later.start_byte)
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))

    def test_no_chunk_exceeds_the_remaining_share_of_a_worker(self) -> None:
        size = 64 * MB
        workers = 4
        scheduler = TransferScheduler(
            origin_url="http://origin/f",
            file_size=size,
            fixed_chunk_size=4 * MB,
            adaptive_chunk_sizing=True,
            workers=workers,
        )
        chunks = _drain(scheduler)

        remaining = size
        for chunk in chunks:
            ceiling = max(MIN_CHUNK_SIZE, remaining // workers)
            self.assertLessEqual(
                chunk.size,
                ceiling,
                f"chunk {chunk.index} of {chunk.size} B exceeds the {ceiling} B "
                f"share with {remaining} B left for {workers} workers",
            )
            remaining -= chunk.size

    def test_floor_keeps_the_pool_occupied(self) -> None:
        """
        The property the floor exists for: enough chunks to go round.

        Without it the controller reaches its 64 MB ceiling on a clean path, so a
        64 MB file becomes a handful of chunks and most of the pool sits idle.
        """
        workers = 8
        scheduler = TransferScheduler(
            origin_url="http://origin/f",
            file_size=64 * MB,
            fixed_chunk_size=4 * MB,
            adaptive_chunk_sizing=True,
            workers=workers,
        )
        chunks = _drain(scheduler)
        self.assertGreaterEqual(
            len(chunks), workers, "fewer chunks than workers leaves the pool idle"
        )

    def test_more_workers_yield_smaller_chunks(self) -> None:
        def largest(workers: int) -> int:
            scheduler = TransferScheduler(
                origin_url="http://origin/f",
                file_size=64 * MB,
                fixed_chunk_size=4 * MB,
                adaptive_chunk_sizing=True,
                workers=workers,
            )
            return max(c.size for c in _drain(scheduler))

        self.assertGreater(largest(2), largest(16))


class PlannerRewindTest(unittest.TestCase):
    def test_rewind_forward_is_rejected(self) -> None:
        planner = DynamicChunkPlanner(file_size=16 * MB)
        planner.next_chunk(None)
        with self.assertRaises(ConfigurationError):
            # Forward would skip bytes rather than re-plan them.
            planner.rewind_to(planner.next_offset + MB, 1)

    def test_rewind_out_of_range_is_rejected(self) -> None:
        planner = DynamicChunkPlanner(file_size=16 * MB)
        with self.assertRaises(ConfigurationError):
            planner.rewind_to(-1, 0)


class ResumeAlignmentTest(unittest.TestCase):
    def test_advance_to_continues_where_an_earlier_session_stopped(self) -> None:
        first = TransferScheduler(
            origin_url="http://origin/f",
            file_size=64 * MB,
            fixed_chunk_size=4 * MB,
            adaptive_chunk_sizing=True,
            workers=4,
        )
        planned = first.plan_initial()
        boundary = planned[-1].end_byte + 1

        resumed = TransferScheduler(
            origin_url="http://origin/f",
            file_size=64 * MB,
            fixed_chunk_size=4 * MB,
            adaptive_chunk_sizing=True,
            workers=4,
        )
        resumed.advance_to(boundary)
        following = resumed.plan_more(1)
        self.assertEqual(following[0].start_byte, boundary)
        self.assertEqual(following[0].index, len(planned))


class MirrorIdentityTest(unittest.TestCase):
    def test_identity_is_stable_across_a_path_change(self) -> None:
        self.assertEqual(
            derive_mirror_id("http://edge.example/a/f.bin", 0),
            derive_mirror_id("http://edge.example/b/other.bin", 0),
        )

    def test_identity_distinguishes_two_urls_on_one_host(self) -> None:
        self.assertNotEqual(
            derive_mirror_id("http://edge.example/f", 0),
            derive_mirror_id("http://edge.example/f", 1),
        )

    def test_single_mirror_pool_is_not_routed(self) -> None:
        """A breaker with no alternative stalls the transfer; see scheduling.py."""
        scheduler = TransferScheduler(
            origin_url="http://only/f",
            file_size=MB,
            fixed_chunk_size=MB,
            adaptive_mirror_routing=True,
        )
        self.assertIsNone(scheduler.dispatcher)
        self.assertTrue(scheduler.stats()["single_mirror_pool"])

    def test_two_mirrors_are_routed(self) -> None:
        scheduler = TransferScheduler(
            origin_url="http://a/f",
            file_size=MB,
            fixed_chunk_size=MB,
            adaptive_mirror_routing=True,
            mirrors=["http://b/f"],
        )
        self.assertIsNotNone(scheduler.dispatcher)


if __name__ == "__main__":
    unittest.main()
