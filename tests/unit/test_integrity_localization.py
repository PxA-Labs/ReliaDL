"""
Sub-block Merkle localization on the download path.

A manifest that carries per-sub-block digests lets a corrupted chunk be repaired
by re-fetching only the part of it that was never verified, instead of the whole
range. These tests pin that behaviour down, including the failure mode that made
the first implementation of it slower than no localization at all: the verified
bytes have to reach disk before the stream is abandoned, or the retry resumes
from an offset whose bytes were never written.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from reliadl.download_engine import DownloadEngine
from reliadl.manifest import (
    DEFAULT_SUB_BLOCK_SIZE,
    calculate_sub_blocks,
    compute_merkle_root,
)
from reliadl.models import DownloadConfig

MB = 1024 * 1024
SUB = DEFAULT_SUB_BLOCK_SIZE


class _CorruptingServer(ThreadingHTTPServer):
    """Serves byte ranges, flipping one byte on the first range that contains it."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, payload: bytes, corrupt_at: Optional[int]) -> None:
        super().__init__(("127.0.0.1", 0), _CorruptingHandler)
        self.payload = payload
        self.corrupt_at = corrupt_at
        self.corrupted_once = False
        self.bytes_served = 0
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/f.bin"

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        # An aborted stream drops the connection mid-body; that is the point.
        pass


class _CorruptingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _CorruptingServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        srv = self.server
        rng = self.headers.get("Range")
        if not rng:
            self.send_response(200)
            self.send_header("Content-Length", str(len(srv.payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"v1"')
            self.end_headers()
            if not head:
                self.wfile.write(srv.payload)
            return

        start_s, _, end_s = rng.split("=", 1)[1].partition("-")
        start, end = int(start_s), min(int(end_s), len(srv.payload) - 1)
        body = bytearray(srv.payload[start:end + 1])
        with srv.lock:
            if (
                srv.corrupt_at is not None
                and not srv.corrupted_once
                and start <= srv.corrupt_at <= end
            ):
                body[srv.corrupt_at - start] ^= 0xFF
                srv.corrupted_once = True
            srv.bytes_served += len(body)

        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(srv.payload)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"v1"')
        self.end_headers()
        if not head:
            self.wfile.write(bytes(body))


class IntegrityLocalizationTest(unittest.IsolatedAsyncioTestCase):
    file_size = 16 * MB
    chunk_size = 4 * MB

    def setUp(self) -> None:
        self.payload = os.urandom(self.file_size)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _manifest(self, *, with_sub_blocks: bool) -> Path:
        chunks = []
        for index, start in enumerate(range(0, self.file_size, self.chunk_size)):
            end = min(start + self.chunk_size, self.file_size) - 1
            data = self.payload[start:end + 1]
            entry = {
                "index": index,
                "start_byte": start,
                "end_byte": end,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            if with_sub_blocks:
                entry["sub_blocks"] = calculate_sub_blocks(data, SUB)
            chunks.append(entry)

        document = {
            "manifest_version": "1.0.0",
            "artifact_metadata": {
                "filename": "f.bin",
                "file_size_bytes": self.file_size,
                "file_hash_sha256": self.digest,
            },
            "chunking_topology": {
                "default_chunk_size_bytes": self.chunk_size,
                "total_chunks": len(chunks),
                "hash_algorithm": "sha256",
                "merkle_tree_root": compute_merkle_root([c["sha256"] for c in chunks]),
            },
            "chunks": chunks,
        }
        path = self.tmp / f"manifest-{with_sub_blocks}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    async def _download(self, *, with_sub_blocks: bool, corrupt_at: Optional[int]) -> tuple[Path, int]:
        manifest = self._manifest(with_sub_blocks=with_sub_blocks)
        server = _CorruptingServer(self.payload, corrupt_at)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        output = self.tmp / f"out-{with_sub_blocks}-{corrupt_at}.bin"
        config = DownloadConfig(
            chunk_size_bytes=self.chunk_size,
            max_parallel_workers=2,
            manifest_path=str(manifest),
            max_retries_per_chunk=4,
            retry_base_delay_seconds=0.0,
            retry_jitter_factor=0.0,
        )
        try:
            await DownloadEngine(config).download(
                server.url, output, expected_hash=self.digest
            )
        finally:
            server.shutdown()
            server.server_close()
        return output, server.bytes_served

    async def test_localized_retry_refetches_only_the_unverified_tail(self) -> None:
        """
        A single corrupt byte must cost the tail of its chunk, not the whole chunk.

        The corruption sits in sub-block 40 of 64, so sub-blocks 0..39 verify and
        are kept; only 24 sub-blocks are re-requested.
        """
        corrupt_at = 2 * self.chunk_size + 40 * SUB + 17
        output, served = await self._download(with_sub_blocks=True, corrupt_at=corrupt_at)

        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.digest)
        wasted = served - self.file_size
        self.assertEqual(
            wasted,
            self.chunk_size - 40 * SUB,
            "retry should re-fetch exactly the sub-blocks from the bad one onward",
        )
        self.assertLess(wasted, self.chunk_size)

    async def test_without_sub_blocks_a_corrupt_byte_costs_the_whole_chunk(self) -> None:
        """The control condition: whole-chunk verification has nothing to localize."""
        corrupt_at = 2 * self.chunk_size + 40 * SUB + 17
        output, served = await self._download(with_sub_blocks=False, corrupt_at=corrupt_at)

        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.digest)
        self.assertEqual(served - self.file_size, self.chunk_size)

    async def test_localization_beats_whole_chunk_verification(self) -> None:
        """The comparison the design exists to win."""
        corrupt_at = 2 * self.chunk_size + 40 * SUB + 17
        _, localized = await self._download(with_sub_blocks=True, corrupt_at=corrupt_at)
        _, whole = await self._download(with_sub_blocks=False, corrupt_at=corrupt_at)
        self.assertLess(localized, whole)

    async def test_corruption_in_the_first_sub_block_saves_nothing(self) -> None:
        """
        Localization saves nothing when the corruption is at the very start.

        This is the honest bound on the technique as implemented: the retry
        resumes *from* the bad sub-block, so the bytes saved are whatever was
        verified before it. Corruption in sub-block 0 verifies nothing, and the
        whole chunk is re-fetched -- the same cost as no localization at all.
        """
        corrupt_at = 2 * self.chunk_size + 5
        output, served = await self._download(with_sub_blocks=True, corrupt_at=corrupt_at)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.digest)
        self.assertEqual(served - self.file_size, self.chunk_size)

    async def test_clean_transfer_with_manifest_wastes_nothing(self) -> None:
        """No corruption means no re-fetch, with or without sub-block digests."""
        output, served = await self._download(with_sub_blocks=True, corrupt_at=None)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), self.digest)
        self.assertEqual(served, self.file_size)

    async def test_manifest_with_mismatched_size_is_rejected(self) -> None:
        """A manifest describing a different payload must not be used."""
        from reliadl.exceptions import PreconditionFailedError

        manifest = json.loads(self._manifest(with_sub_blocks=True).read_text(encoding="utf-8"))
        manifest["artifact_metadata"]["file_size_bytes"] = self.file_size + 1
        path = self.tmp / "bad-size.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        server = _CorruptingServer(self.payload, None)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        try:
            config = DownloadConfig(
                chunk_size_bytes=self.chunk_size,
                max_parallel_workers=2,
                manifest_path=str(path),
            )
            with self.assertRaises(PreconditionFailedError):
                await DownloadEngine(config).download(server.url, self.tmp / "nope.bin")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
