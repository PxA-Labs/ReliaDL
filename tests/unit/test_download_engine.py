"""
Unit tests for the asynchronous range download engine in reliadl.download_engine.

A real threaded HTTP server on localhost serves byte ranges, so the tests cover
the httpx client, the probe, the worker pool, positional writes, checkpointing,
graceful shutdown, and resume end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from reliadl.cli import main as cli_main
from reliadl.download_engine import (
    PART_SUFFIX,
    DownloadEngine,
    _parse_content_range,
    plan_chunks,
)
from reliadl.exceptions import (
    ClientError,
    DownloadCancelledError,
    FileHashMismatchError,
    PreconditionFailedError,
    RangeNotSupportedError,
)
from reliadl.models import ChunkStatus, DownloadConfig, DownloadStatus, ProgressReport
from reliadl.state_manager import StateManager

MB = 1024 * 1024


class _RangeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, payload: bytes) -> None:
        super().__init__(("127.0.0.1", 0), _RangeHandler)
        self.payload = payload
        self.etag = '"v1"'
        self.allow_head = True
        self.ranges = True
        self.chunk_delay = 0.0
        # start byte -> HTTP status to answer once, then serve normally
        self.fail_once: dict[int, int] = {}
        # ranged GETs at or beyond this offset wait until the gate opens
        self.gate_from: Optional[int] = None
        self.gate = threading.Event()
        self.ranged_bytes_served = 0
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/file.bin"

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        # Cancelled workers drop their connections mid-body; that is expected.
        pass


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _RangeServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        if not self.server.allow_head:
            self._send(405, b"", head=True)
            return
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _send(self, status: int, body: bytes, head: bool, headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _serve(self, head: bool) -> None:
        srv = self.server
        payload = srv.payload
        headers = {"ETag": srv.etag}
        if srv.ranges:
            headers["Accept-Ranges"] = "bytes"

        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if not (range_header and srv.ranges) or (if_range and if_range != srv.etag):
            self._send(200, payload, head, headers)
            return

        start_s, _, end_s = range_header.split("=", 1)[1].partition("-")
        start, end = int(start_s), min(int(end_s), len(payload) - 1)

        status = srv.fail_once.pop(start, None)
        if status is not None:
            self._send(status, b"", head)
            return

        with srv._lock:
            srv._active += 1
            srv.max_active = max(srv.max_active, srv._active)
        try:
            if srv.gate_from is not None and start >= srv.gate_from:
                srv.gate.wait(timeout=10)
            if srv.chunk_delay:
                time.sleep(srv.chunk_delay)
            body = payload[start:end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
            self._send(206, body, head, headers)
            if not head and end > 0:
                with srv._lock:
                    srv.ranged_bytes_served += len(body)
        finally:
            with srv._lock:
                srv._active -= 1


def _config(**overrides: object) -> DownloadConfig:
    values: dict[str, object] = {
        "chunk_size_bytes": MB,
        "max_parallel_workers": 4,
        "max_retries_per_chunk": 3,
        "retry_base_delay_seconds": 0.0,
        "retry_jitter_factor": 0.0,
        "progress_update_interval_seconds": 0.001,
        "http2": False,
    }
    values.update(overrides)
    return DownloadConfig(**values)


class DownloadEngineTestBase(unittest.IsolatedAsyncioTestCase):
    payload_size = 5 * MB + 12345

    def setUp(self) -> None:
        self.payload = os.urandom(self.payload_size)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.server = _RangeServer(self.payload)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.output = self.tmp / "out.bin"

    def tearDown(self) -> None:
        self.server.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    @property
    def state_path(self) -> Path:
        return StateManager().get_state_path(self.output)


class TestDownload(DownloadEngineTestBase):
    async def test_parallel_download_matches_source(self) -> None:
        self.server.chunk_delay = 0.05
        result = await DownloadEngine(_config()).download(self.server.url, self.output)

        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(result.file_hash, self.digest)
        self.assertEqual(result.total_chunks, 6)
        self.assertEqual(result.total_bytes_downloaded, self.payload_size)
        self.assertGreaterEqual(self.server.max_active, 2, "chunks were not fetched concurrently")
        self.assertLessEqual(self.server.max_active, 4, "worker pool exceeded its bound")
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.state_path.parent.exists())
        self.assertFalse(Path(str(self.output) + PART_SUFFIX).exists())

    async def test_expected_hash_is_verified(self) -> None:
        result = await DownloadEngine(_config()).download(
            self.server.url, self.output, expected_hash=f"sha256:{self.digest.upper()}"
        )
        self.assertTrue(result.is_verified)

    async def test_expected_hash_mismatch_keeps_partial_state(self) -> None:
        with self.assertRaises(FileHashMismatchError):
            await DownloadEngine(_config()).download(self.server.url, self.output, expected_hash="00" * 32)
        self.assertFalse(self.output.exists())
        self.assertEqual(StateManager().load(self.state_path).status, DownloadStatus.FAILED)

    async def test_probe_falls_back_to_range_get_when_head_is_rejected(self) -> None:
        self.server.allow_head = False
        await DownloadEngine(_config()).download(self.server.url, self.output)
        self.assertEqual(self.output.read_bytes(), self.payload)

    async def test_server_without_ranges_is_rejected(self) -> None:
        self.server.ranges = False
        with self.assertRaises(RangeNotSupportedError):
            await DownloadEngine(_config()).download(self.server.url, self.output)
        self.assertFalse(self.output.exists())

    async def test_transient_server_error_is_retried(self) -> None:
        self.server.fail_once = {2 * MB: 503, 4 * MB: 429}
        result = await DownloadEngine(_config()).download(self.server.url, self.output)
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(result.chunks_retried, 2)

    async def test_permanent_client_error_aborts_and_checkpoints(self) -> None:
        self.server.fail_once = {3 * MB: 404}
        with self.assertRaises(ClientError):
            await DownloadEngine(_config()).download(self.server.url, self.output)

        state = StateManager().load(self.state_path)
        self.assertEqual(state.status, DownloadStatus.FAILED)
        self.assertEqual(state.chunks[3].status, ChunkStatus.ABANDONED)
        self.assertNotIn(ChunkStatus.IN_PROGRESS, {c.status for c in state.chunks})

        # The failure was transient after all; resume finishes the job.
        result = await DownloadEngine(_config()).resume(self.state_path)
        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(result.file_hash, self.digest)

    async def test_progress_reports_reach_completion(self) -> None:
        reports: list[ProgressReport] = []
        await DownloadEngine(_config(), progress_callback=reports.append).download(self.server.url, self.output)
        self.assertTrue(reports)
        self.assertEqual(reports[-1].downloaded_bytes, self.payload_size)
        self.assertEqual(reports[-1].chunks_complete, 6)
        percentages = [r.percentage for r in reports]
        self.assertEqual(percentages, sorted(percentages))


class TestShutdownAndResume(DownloadEngineTestBase):
    async def _interrupt_after_first_chunk(self) -> None:
        """Start a download, let chunk 0 finish, then request a graceful stop."""
        self.server.gate_from = MB  # every chunk but the first stalls

        engine: DownloadEngine

        def on_progress(report: ProgressReport) -> None:
            if report.chunks_complete >= 1:
                engine.request_shutdown()

        engine = DownloadEngine(_config(), progress_callback=on_progress)
        with self.assertRaises(DownloadCancelledError) as ctx:
            await asyncio.wait_for(engine.download(self.server.url, self.output), timeout=10)
        self.assertEqual(ctx.exception.state_file, str(self.state_path))

    async def test_shutdown_checkpoints_completed_chunks(self) -> None:
        await self._interrupt_after_first_chunk()

        state = StateManager().load(self.state_path)
        self.assertEqual(state.status, DownloadStatus.CANCELLED)
        self.assertEqual(state.chunks[0].status, ChunkStatus.COMPLETE)
        self.assertEqual(state.chunks[0].computed_hash, hashlib.sha256(self.payload[:MB]).hexdigest())
        self.assertTrue(all(c.status == ChunkStatus.PENDING for c in state.chunks[1:]))
        self.assertFalse(self.output.exists())

    async def test_resume_fetches_only_missing_chunks(self) -> None:
        await self._interrupt_after_first_chunk()
        self.server.gate.set()
        self.server.gate_from = None
        served_before = self.server.ranged_bytes_served

        result = await DownloadEngine(_config()).resume(self.state_path)

        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(result.total_bytes_downloaded, self.payload_size - MB)
        self.assertEqual(self.server.ranged_bytes_served - served_before, self.payload_size - MB)
        self.assertFalse(self.state_path.exists())

    async def test_resume_refetches_chunk_corrupted_on_disk(self) -> None:
        await self._interrupt_after_first_chunk()
        self.server.gate.set()
        self.server.gate_from = None
        part = Path(str(self.output) + PART_SUFFIX)
        with open(part, "r+b") as f:
            f.seek(100)
            f.write(b"\x00" * 16)

        result = await DownloadEngine(_config()).resume(self.state_path)

        self.assertEqual(self.output.read_bytes(), self.payload)
        self.assertEqual(result.total_bytes_downloaded, self.payload_size)

    async def test_resume_rejects_changed_remote_file(self) -> None:
        await self._interrupt_after_first_chunk()
        self.server.gate.set()
        self.server.gate_from = None
        self.server.etag = '"v2"'

        with self.assertRaises(PreconditionFailedError):
            await DownloadEngine(_config()).resume(self.state_path)
        self.assertFalse(self.output.exists())

    async def test_cancelling_the_task_checkpoints_state(self) -> None:
        self.server.gate_from = MB
        task = asyncio.create_task(DownloadEngine(_config()).download(self.server.url, self.output))
        while not self.state_path.exists() or StateManager().load(self.state_path).chunks[0].status != ChunkStatus.COMPLETE:
            await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = StateManager().load(self.state_path)
        self.assertEqual(state.status, DownloadStatus.CANCELLED)
        self.assertEqual(state.chunks[0].status, ChunkStatus.COMPLETE)


class TestCli(DownloadEngineTestBase):
    payload_size = 2 * MB + 7

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(list(argv))
        return code, out.getvalue()

    def test_download_command(self) -> None:
        code, out = self._run(
            "download", "--url", self.server.url, "-o", str(self.output),
            "--workers", "2", "--chunk-size", "1MB", "--sha256", self.digest,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("[SUCCESS]", out)
        self.assertIn(f"SHA-256 (verified): {self.digest}", out)
        self.assertEqual(self.output.read_bytes(), self.payload)

    def test_download_command_reports_errors(self) -> None:
        self.server.ranges = False
        code, out = self._run("download", "--url", self.server.url, "-o", str(self.output))
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", out)

    def test_invalid_chunk_size_is_rejected(self) -> None:
        code, out = self._run("download", "--url", self.server.url, "-o", str(self.output), "--chunk-size", "1KB")
        self.assertEqual(code, 1)
        self.assertIn("Invalid configuration", out)

    def test_resume_command(self) -> None:
        self.server.fail_once = {MB: 404}
        code, _ = self._run("download", "--url", self.server.url, "-o", str(self.output), "--chunk-size", "1MB")
        self.assertEqual(code, 1)
        code, out = self._run("resume", "--state-file", str(self.state_path))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.output.read_bytes(), self.payload)


class TestHelpers(unittest.TestCase):
    def test_plan_chunks_covers_file_without_gaps(self) -> None:
        chunks = plan_chunks(10 * MB + 1, 4 * MB)
        self.assertEqual([(c.start_byte, c.end_byte) for c in chunks], [
            (0, 4 * MB - 1), (4 * MB, 8 * MB - 1), (8 * MB, 10 * MB),
        ])
        self.assertEqual(sum(c.size for c in chunks), 10 * MB + 1)

    def test_plan_chunks_exact_multiple_and_empty(self) -> None:
        self.assertEqual(len(plan_chunks(8 * MB, 4 * MB)), 2)
        self.assertEqual(plan_chunks(0, 4 * MB), [])

    def test_parse_content_range(self) -> None:
        self.assertEqual(_parse_content_range("bytes 0-0/1234"), (0, 0, 1234))
        self.assertEqual(_parse_content_range("bytes 10-19/*"), (10, 19, None))
        self.assertIsNone(_parse_content_range("items 0-1/2"))
        self.assertIsNone(_parse_content_range(None))


if __name__ == "__main__":
    unittest.main()
