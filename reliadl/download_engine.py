"""
Asynchronous HTTP range download engine for ReliaDL.

Probes the origin for range support, splits the file into fixed-size chunks,
and fetches them concurrently over a pooled ``httpx.AsyncClient``. Each chunk
is streamed straight into a pre-allocated ``<output>.part`` file with
positional writes, hashed on the fly, and checkpointed to the session state
file once its bytes are on disk. An interrupted session (Ctrl+C, SIGTERM, a
chunk that exhausts its retries) leaves a state file that ``resume`` picks up,
re-verifying every chunk it claims to have before trusting it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

import httpx

from reliadl.exceptions import (
    ChunkHashMismatchError,
    ClientError,
    ConnectionError,
    DownloadCancelledError,
    FileHashMismatchError,
    HTTPError,
    NetworkError,
    PreconditionFailedError,
    RangeNotSupportedError,
    ReliaDLError,
    ServerError,
    TimeoutError,
)
from reliadl.hash_verifier import (
    StreamingHashVerifier,
    compute_file_hash,
    constant_time_compare,
    normalize_hash,
)
from reliadl.logger import get_logger
from reliadl.models import (
    ChunkState,
    ChunkStatus,
    DownloadConfig,
    DownloadResult,
    DownloadState,
    DownloadStatus,
    ProgressReport,
)
from reliadl.rate_limiter import TokenBucketRateLimiter
from reliadl.sparse_writer import SparseFileWriter
from reliadl.state_manager import StateManager

logger = get_logger("reliadl.download_engine")

PART_SUFFIX = ".part"
_REVALIDATE_READ_SIZE = 1024 * 1024

ProgressCallback = Callable[[ProgressReport], None]


@dataclass(frozen=True)
class RemoteFileInfo:
    """What a HEAD (or one-byte range) probe learned about the remote file."""

    url: str
    size: Optional[int]
    accepts_ranges: bool
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def if_range(self) -> Optional[str]:
        """Validator for ``If-Range``; RFC 9110 only allows a strong ETag or a date."""
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified


def plan_chunks(file_size: int, chunk_size: int) -> list[ChunkState]:
    """Split ``file_size`` bytes into contiguous, non-overlapping chunk states."""
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    return [
        ChunkState(index=index, start_byte=start, end_byte=min(start + chunk_size, file_size) - 1)
        for index, start in enumerate(range(0, file_size, chunk_size))
    ]


def _parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _parse_content_range(value: Optional[str]) -> Optional[tuple[int, int, Optional[int]]]:
    """Parse ``bytes <start>-<end>/<total|*>`` into ``(start, end, total)``."""
    if not value or not value.lower().startswith("bytes "):
        return None
    try:
        span, _, total = value[6:].strip().partition("/")
        start, _, end = span.partition("-")
        return int(start), int(end), None if total in ("", "*") else int(total)
    except ValueError:
        return None


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _http_error(response: httpx.Response, url: str) -> HTTPError:
    """Map a non-success response onto the ReliaDL HTTP error hierarchy."""
    status = response.status_code
    message = f"HTTP {status} {response.reason_phrase} from {url}"
    headers = dict(response.headers)
    if status == 412:
        return PreconditionFailedError(message, etag=response.headers.get("etag"), url=url)
    if 400 <= status < 500:
        return ClientError(
            message,
            retry_after=_retry_after_seconds(response),
            status_code=status,
            response_headers=headers,
            url=url,
        )
    if status >= 500:
        return ServerError(message, status_code=status, response_headers=headers, url=url)
    return HTTPError(message, status_code=status, response_headers=headers, url=url, is_retryable=False)


def _transport_error(err: httpx.HTTPError, url: str) -> NetworkError:
    """Map an httpx transport failure onto the ReliaDL network error hierarchy."""
    if isinstance(err, httpx.TimeoutException):
        return TimeoutError(f"Timed out talking to {url}: {err!r}", url=url, cause=err)
    if isinstance(err, httpx.ConnectError):
        return ConnectionError(f"Could not connect to {url}: {err}", host=httpx.URL(url).host, url=url, cause=err)
    return NetworkError(f"Transfer from {url} failed: {err!r}", url=url, cause=err)


class DownloadEngine:
    """
    Parallel range downloader bounded by ``config.max_parallel_workers``.

    One engine runs one session at a time. ``request_shutdown`` may be called
    from a signal handler on the engine's event loop to stop the session
    cleanly; the awaiting ``download``/``resume`` call then raises
    ``DownloadCancelledError`` after the state file has been written.
    """

    def __init__(
        self,
        config: Optional[DownloadConfig] = None,
        *,
        state_manager: Optional[StateManager] = None,
        progress_callback: Optional[ProgressCallback] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._config = config if config is not None else DownloadConfig()
        self._state_manager = state_manager if state_manager is not None else StateManager(
            default_state_dir=self._config.state_directory,
        )
        self._progress_callback = progress_callback
        self._transport = transport
        self._shutdown = asyncio.Event()
        self._limiter: Optional[TokenBucketRateLimiter] = None
        if self._config.max_bandwidth_bytes_per_sec > 0:
            self._limiter = TokenBucketRateLimiter(float(self._config.max_bandwidth_bytes_per_sec))
        # Per-session progress accounting; bytes of chunks still in flight are
        # tracked separately so a retried chunk does not count twice.
        self._in_flight: dict[int, int] = {}
        self._state: Optional[DownloadState] = None
        self._reset_progress()

    @property
    def config(self) -> DownloadConfig:
        return self._config

    def request_shutdown(self) -> None:
        """Stop dispatching chunks, abort in-flight ones, and checkpoint state."""
        self._shutdown.set()

    # ── Public entry points ────────────────────────────────────────────────

    async def download(
        self,
        url: str,
        output_path: Union[str, Path],
        expected_hash: Optional[str] = None,
    ) -> DownloadResult:
        """Download ``url`` to ``output_path``, starting a fresh session."""
        output = Path(output_path).resolve()
        part_path = output.with_name(output.name + PART_SUFFIX)
        expected = normalize_hash(expected_hash) if expected_hash else None

        async with self._build_client() as client:
            info = await self.probe(client, url)
            if not info.accepts_ranges or info.size is None:
                raise RangeNotSupportedError(
                    f"{url} does not support byte-range requests "
                    "(no 'Accept-Ranges: bytes' and no 206 reply to a range probe)",
                    url=url,
                )

            state = DownloadState(
                download_id=uuid.uuid4().hex,
                url=url,
                target_path=str(output),
                file_size=info.size,
                chunk_size=self._config.chunk_size_bytes,
                hash_algorithm=self._config.hash_algorithm,
                expected_file_hash=expected,
                etag=info.etag,
                output_path=str(part_path),
                status=DownloadStatus.DOWNLOADING,
                chunks=plan_chunks(info.size, self._config.chunk_size_bytes),
            )
            # A leftover .part from an abandoned session holds unrelated bytes.
            part_path.unlink(missing_ok=True)
            writer = SparseFileWriter(
                part_path,
                info.size,
                check_disk_space=self._config.pre_check_disk_space,
            )
            state_path = self._state_manager.get_state_path(output)
            return await self._run(client, info, state, state_path, writer)

    async def resume(self, state_path: Union[str, Path]) -> DownloadResult:
        """Continue the session recorded in ``state_path``."""
        path = Path(state_path).resolve()
        state = self._state_manager.load(path)
        part_path = Path(state.output_path or state.target_path + PART_SUFFIX)

        async with self._build_client() as client:
            info = await self.probe(client, state.url)
            if not info.accepts_ranges or info.size is None:
                raise RangeNotSupportedError(
                    f"{state.url} no longer supports byte-range requests",
                    url=state.url,
                )
            if info.size != state.file_size or (
                state.etag and info.etag and state.etag != info.etag
            ):
                raise PreconditionFailedError(
                    f"Remote file changed since the session started "
                    f"(size {state.file_size} -> {info.size}, "
                    f"ETag {state.etag} -> {info.etag}); start a new download",
                    etag=info.etag,
                    url=state.url,
                )

            if not part_path.is_file():
                for chunk in state.chunks:
                    self._reset_chunk(chunk)
            writer = SparseFileWriter(part_path, state.file_size, check_disk_space=False)
            await asyncio.to_thread(self._revalidate_chunks, state, writer)
            state.status = DownloadStatus.DOWNLOADING
            return await self._run(client, info, state, path, writer)

    async def probe(self, client: httpx.AsyncClient, url: str) -> RemoteFileInfo:
        """
        Discover size, range support, and validators for ``url``.

        HEAD is tried first. Servers that reject HEAD or do not advertise
        ``Accept-Ranges`` get a one-byte range GET, whose 206 and
        ``Content-Range`` total settle both questions at once.
        """
        try:
            head = await client.head(url)
        except httpx.HTTPError as err:
            raise _transport_error(err, url) from err

        if head.is_success:
            size = _parse_int(head.headers.get("content-length"))
            if size is not None and head.headers.get("accept-ranges", "").lower() == "bytes":
                return RemoteFileInfo(
                    url=str(head.url),
                    size=size,
                    accepts_ranges=True,
                    etag=head.headers.get("etag"),
                    last_modified=head.headers.get("last-modified"),
                )

        try:
            async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                etag = response.headers.get("etag")
                last_modified = response.headers.get("last-modified")
                if response.status_code == 206:
                    parsed = _parse_content_range(response.headers.get("content-range"))
                    total = parsed[2] if parsed else None
                    return RemoteFileInfo(
                        url=str(response.url),
                        size=total,
                        accepts_ranges=total is not None,
                        etag=etag,
                        last_modified=last_modified,
                    )
                if not response.is_success:
                    raise _http_error(response, url)
                return RemoteFileInfo(
                    url=str(response.url),
                    size=_parse_int(response.headers.get("content-length")),
                    accepts_ranges=False,
                    etag=etag,
                    last_modified=last_modified,
                )
        except httpx.HTTPError as err:
            raise _transport_error(err, url) from err

    # ── Session orchestration ──────────────────────────────────────────────

    async def _run(
        self,
        client: httpx.AsyncClient,
        info: RemoteFileInfo,
        state: DownloadState,
        state_path: Path,
        writer: SparseFileWriter,
    ) -> DownloadResult:
        self._shutdown.clear()
        self._reset_progress()
        self._total_bytes = state.file_size
        self._completed_bytes = sum(c.size for c in state.completed_chunks)
        self._resumed_bytes = self._completed_bytes
        self._state = state

        queue: asyncio.Queue[ChunkState] = asyncio.Queue()
        for chunk in state.chunks:
            if not chunk.status.is_successful:
                self._reset_chunk(chunk)
                queue.put_nowait(chunk)

        self._state_manager.save(state, state_path)
        worker_count = min(self._config.max_parallel_workers, max(1, queue.qsize()))
        logger.info(
            "download.start",
            url=state.url,
            size=state.file_size,
            chunks=state.total_chunks,
            pending=queue.qsize(),
            workers=worker_count,
        )

        workers = [
            asyncio.create_task(self._worker(client, info, queue, state, state_path, writer))
            for _ in range(worker_count if queue.qsize() else 0)
        ]
        shutdown_waiter = asyncio.create_task(self._shutdown.wait())
        try:
            running = set(workers)
            while running:
                await asyncio.wait({*running, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
                running = {w for w in workers if not w.done()}
                failed = next((w for w in workers if w.done() and not w.cancelled() and w.exception()), None)
                if failed is not None:
                    await self._stop_workers(workers)
                    self._checkpoint_interrupted(state, state_path, writer, status=DownloadStatus.FAILED)
                    raise failed.exception()  # type: ignore[misc]
                if running and self._shutdown.is_set():
                    await self._stop_workers(workers)
                    self._checkpoint_interrupted(state, state_path, writer)
                    raise DownloadCancelledError(
                        f"Download interrupted; resume with: reliadl resume --state-file {state_path}",
                        state_file=str(state_path),
                    )
        except asyncio.CancelledError:
            await self._stop_workers(workers)
            self._checkpoint_interrupted(state, state_path, writer)
            raise
        finally:
            shutdown_waiter.cancel()

        return await self._finalize(state, state_path, writer)

    async def _worker(
        self,
        client: httpx.AsyncClient,
        info: RemoteFileInfo,
        queue: asyncio.Queue[ChunkState],
        state: DownloadState,
        state_path: Path,
        writer: SparseFileWriter,
    ) -> None:
        while not self._shutdown.is_set():
            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._active_workers += 1
            try:
                await self._download_chunk_with_retries(client, info, chunk, state.hash_algorithm, writer)
            finally:
                self._active_workers -= 1
            # The state file may only claim bytes that are durable on disk.
            writer.sync()
            self._state_manager.save(state, state_path)

    async def _download_chunk_with_retries(
        self,
        client: httpx.AsyncClient,
        info: RemoteFileInfo,
        chunk: ChunkState,
        algorithm: str,
        writer: SparseFileWriter,
    ) -> None:
        max_attempts = max(1, self._config.max_retries_per_chunk)
        while True:
            chunk.mark_in_progress()
            try:
                digest = await self._fetch_chunk(client, info, chunk, algorithm, writer)
            except ReliaDLError as err:
                self._in_flight.pop(chunk.index, None)
                if not err.is_retryable or chunk.retries + 1 >= max_attempts:
                    chunk.mark_abandoned(str(err))
                    logger.error("chunk.abandoned", chunk=chunk.index, attempts=chunk.retries + 1, error=str(err))
                    raise
                chunk.mark_failed(str(err))
                delay = self._backoff_delay(chunk.retries, err)
                logger.warning("chunk.retry", chunk=chunk.index, attempt=chunk.retries, delay=round(delay, 2), error=str(err))
                await asyncio.sleep(delay)
                continue
            chunk.mark_complete(digest)
            self._in_flight.pop(chunk.index, None)
            self._completed_bytes += chunk.size
            self._emit_progress(force=True)
            return

    async def _fetch_chunk(
        self,
        client: httpx.AsyncClient,
        info: RemoteFileInfo,
        chunk: ChunkState,
        algorithm: str,
        writer: SparseFileWriter,
    ) -> str:
        headers = {"Range": f"bytes={chunk.start_byte}-{chunk.end_byte}"}
        if info.if_range:
            headers["If-Range"] = info.if_range

        hasher = StreamingHashVerifier(algorithm)
        offset = chunk.start_byte
        self._in_flight[chunk.index] = 0
        try:
            async with client.stream("GET", info.url, headers=headers) as response:
                if response.status_code == 200:
                    # A 200 to a ranged request means the server ignored the
                    # range, or If-Range failed because the file changed.
                    if info.if_range:
                        raise PreconditionFailedError(
                            f"Remote file changed during download (If-Range {info.if_range} no longer matches)",
                            etag=response.headers.get("etag"),
                            url=info.url,
                        )
                    raise RangeNotSupportedError(
                        f"Server ignored the byte range for chunk {chunk.index}",
                        url=info.url,
                    )
                if response.status_code != 206:
                    raise _http_error(response, info.url)

                parsed = _parse_content_range(response.headers.get("content-range"))
                if parsed is None or parsed[:2] != (chunk.start_byte, chunk.end_byte):
                    raise HTTPError(
                        f"Chunk {chunk.index}: requested bytes {chunk.start_byte}-{chunk.end_byte}, "
                        f"server sent Content-Range {response.headers.get('content-range')!r}",
                        status_code=206,
                        url=info.url,
                        is_retryable=False,
                    )

                async for data in response.aiter_raw():
                    if offset + len(data) > chunk.end_byte + 1:
                        raise HTTPError(
                            f"Chunk {chunk.index}: server sent more bytes than the requested range",
                            status_code=206,
                            url=info.url,
                            is_retryable=False,
                        )
                    await self._throttle(len(data))
                    writer.write_at(offset, data)
                    hasher.update(data)
                    offset += len(data)
                    self._in_flight[chunk.index] = offset - chunk.start_byte
                    self._emit_progress()
        except httpx.HTTPError as err:
            raise _transport_error(err, info.url) from err

        received = offset - chunk.start_byte
        if received != chunk.size:
            raise NetworkError(
                f"Chunk {chunk.index} truncated: received {received} of {chunk.size} bytes",
                url=info.url,
            )

        digest = hasher.hexdigest()
        if chunk.expected_hash and not constant_time_compare(digest, chunk.expected_hash):
            raise ChunkHashMismatchError(
                f"Chunk {chunk.index} failed hash verification",
                chunk_index=chunk.index,
                expected_hash=chunk.expected_hash,
                computed_hash=digest,
                start_byte=chunk.start_byte,
                end_byte=chunk.end_byte,
            )
        return digest

    async def _finalize(
        self,
        state: DownloadState,
        state_path: Path,
        writer: SparseFileWriter,
    ) -> DownloadResult:
        await asyncio.to_thread(writer.sync)
        writer.close()
        part_path = writer.target_path
        output = Path(state.target_path)

        file_hash = ""
        verified = False
        if self._config.verify_on_complete or state.expected_file_hash:
            state.status = DownloadStatus.VERIFYING
            file_hash = await asyncio.to_thread(compute_file_hash, part_path, state.hash_algorithm)
            if state.expected_file_hash:
                if not constant_time_compare(file_hash, state.expected_file_hash):
                    state.status = DownloadStatus.FAILED
                    self._state_manager.save(state, state_path)
                    raise FileHashMismatchError(
                        f"Downloaded file hash {file_hash} does not match expected {state.expected_file_hash}",
                        context={"path": str(part_path)},
                    )
                verified = True

        os.replace(part_path, output)
        self._state_manager.delete(state_path)
        try:
            state_path.parent.rmdir()  # only succeeds when no other session uses it
        except OSError as exc:
            logger.debug("download.cleanup_state_dir_failed", path=str(state_path.parent), error=str(exc))
        state.status = DownloadStatus.COMPLETE
        self._emit_progress(force=True)

        elapsed = time.monotonic() - self._started_at
        fetched = state.file_size - self._resumed_bytes
        logger.info("download.complete", path=str(output), size=state.file_size, seconds=round(elapsed, 3))
        return DownloadResult(
            output_path=output,
            file_hash=file_hash,
            is_verified=verified,
            file_size=state.file_size,
            total_chunks=state.total_chunks,
            chunks_retried=sum(1 for c in state.chunks if c.retries),
            total_bytes_downloaded=fetched,
            elapsed_seconds=elapsed,
            average_speed_bps=fetched / elapsed if elapsed > 0 else 0.0,
            download_id=state.download_id,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _build_client(self) -> httpx.AsyncClient:
        cfg = self._config
        return httpx.AsyncClient(
            transport=self._transport,
            http2=cfg.http2 and importlib.util.find_spec("h2") is not None,
            verify=cfg.verify_ssl,
            proxy=cfg.proxy_url,
            follow_redirects=True,
            max_redirects=cfg.max_redirects,
            timeout=httpx.Timeout(
                connect=cfg.connect_timeout_seconds,
                read=cfg.read_timeout_seconds,
                write=cfg.read_timeout_seconds,
                pool=None,
            ),
            limits=httpx.Limits(
                max_connections=cfg.max_parallel_workers,
                max_keepalive_connections=cfg.max_parallel_workers,
            ),
            # Content-Length and byte offsets must describe the stored bytes,
            # not a compressed encoding of them.
            headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "identity"},
        )

    async def _stop_workers(self, workers: list[asyncio.Task[None]]) -> None:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def _checkpoint_interrupted(
        self,
        state: DownloadState,
        state_path: Path,
        writer: SparseFileWriter,
        status: DownloadStatus = DownloadStatus.CANCELLED,
    ) -> None:
        for chunk in state.chunks:
            if chunk.status in (ChunkStatus.IN_PROGRESS, ChunkStatus.DOWNLOADING, ChunkStatus.FAILED):
                chunk.status = ChunkStatus.PENDING
        state.status = status
        writer.sync()
        writer.close()
        self._state_manager.save(state, state_path)
        logger.info("download.checkpoint", state_file=str(state_path), status=status.value)

    def _revalidate_chunks(self, state: DownloadState, writer: SparseFileWriter) -> None:
        """Re-hash chunks the state file calls complete; only matching ones are kept."""
        for chunk in state.chunks:
            if not chunk.status.is_successful:
                continue
            if not chunk.computed_hash:
                self._reset_chunk(chunk)
                continue
            hasher = StreamingHashVerifier(state.hash_algorithm)
            for offset in range(chunk.start_byte, chunk.end_byte + 1, _REVALIDATE_READ_SIZE):
                hasher.update(writer.read_at(offset, min(_REVALIDATE_READ_SIZE, chunk.end_byte + 1 - offset)))
            if not hasher.verify(chunk.computed_hash):
                logger.warning("chunk.revalidation_failed", chunk=chunk.index)
                self._reset_chunk(chunk)

    @staticmethod
    def _reset_chunk(chunk: ChunkState) -> None:
        if chunk.status == ChunkStatus.ABANDONED:
            chunk.retries = 0
        chunk.status = ChunkStatus.PENDING
        chunk.hash_verified = False
        chunk.computed_hash = None

    def _backoff_delay(self, attempt: int, err: ReliaDLError) -> float:
        cfg = self._config
        delay = min(
            cfg.retry_max_delay_seconds,
            cfg.retry_base_delay_seconds * cfg.retry_backoff_factor ** max(0, attempt - 1),
        )
        delay += random.uniform(0.0, cfg.retry_jitter_factor * delay)
        retry_after = getattr(err, "retry_after", None)
        if retry_after is not None:
            delay = max(delay, min(float(retry_after), cfg.retry_max_delay_seconds))
        return delay

    async def _throttle(self, amount: int) -> None:
        if self._limiter is None:
            return
        # A single read can exceed the bucket when the rate is very low.
        step = max(1, int(self._limiter.capacity_bytes))
        while amount > 0:
            take = min(step, amount)
            await self._limiter.acquire(take)
            amount -= take

    def _reset_progress(self) -> None:
        self._started_at = time.monotonic()
        self._total_bytes = 0
        self._completed_bytes = 0
        self._resumed_bytes = 0
        self._in_flight = {}
        self._active_workers = 0
        self._last_emit = 0.0
        self._last_emit_bytes = 0
        self._state = None

    def _emit_progress(self, force: bool = False) -> None:
        if self._progress_callback is None or self._state is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self._config.progress_update_interval_seconds:
            return

        downloaded = self._completed_bytes + sum(self._in_flight.values())
        elapsed = now - self._started_at
        interval = now - self._last_emit if self._last_emit else elapsed
        fetched = downloaded - self._resumed_bytes
        current_speed = (downloaded - self._last_emit_bytes) / interval if self._last_emit and interval > 0 else (
            fetched / elapsed if elapsed > 0 else 0.0
        )
        average_speed = fetched / elapsed if elapsed > 0 else 0.0
        remaining = self._total_bytes - downloaded
        self._last_emit = now
        self._last_emit_bytes = downloaded

        chunks = self._state.chunks
        self._progress_callback(
            ProgressReport(
                download_id=self._state.download_id,
                timestamp=datetime.now(timezone.utc),
                total_bytes=self._total_bytes,
                downloaded_bytes=downloaded,
                percentage=100.0 * downloaded / self._total_bytes if self._total_bytes else 100.0,
                total_chunks=len(chunks),
                chunks_complete=sum(1 for c in chunks if c.status.is_successful),
                chunks_in_progress=sum(1 for c in chunks if c.status == ChunkStatus.IN_PROGRESS),
                chunks_failed=sum(1 for c in chunks if c.status in (ChunkStatus.FAILED, ChunkStatus.ABANDONED)),
                chunks_pending=sum(1 for c in chunks if c.status == ChunkStatus.PENDING),
                current_speed_bps=current_speed,
                average_speed_bps=average_speed,
                elapsed_seconds=elapsed,
                estimated_remaining_seconds=remaining / average_speed if average_speed > 0 else None,
                active_workers=self._active_workers,
            )
        )
