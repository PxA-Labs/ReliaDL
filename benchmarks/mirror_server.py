"""
Configurable fault-injecting HTTP origin for ReliaDL experiments.

Why an emulated mirror rather than `tc`/netem
---------------------------------------------
Three reasons, and the third is the one that decided it.

1. It is exactly reproducible. Every impairment is driven by a seeded PRNG keyed
   on the byte range being served, so the same scenario and seed produce the same
   impairments on any machine and any OS -- including the Windows machines this
   project is developed on, where netem does not exist.
2. It measures what it impairs. The server counts the bytes it actually writes to
   the socket, so "bytes wasted on re-download" is observed rather than inferred.
3. Silent byte corruption at a controlled rate is the central variable in these
   experiments, and `tc` cannot produce it. netem corrupts frames, which TCP's
   checksum catches and retransmits; what matters here is corruption that reaches
   the application intact-looking, which is what a faulty disk, a buggy proxy or a
   hostile mirror actually delivers.

The cost is that this is emulation, not a real network stack. `validate_against_netem.py`
exists to answer that objection: it runs a reduced scenario set under real kernel
shaping on Linux and compares the distributions.

Determinism
-----------
Impairments derive from `hash(seed, start_byte, attempt)`, not from a stream of
random draws, so they do not depend on request arrival order. Two runs with the
same seed corrupt the same bytes and drop the same requests even when the client
schedules its workers differently -- which is exactly what is needed to compare
two schedulers fairly.
"""

from __future__ import annotations

import hashlib
import struct
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


@dataclass
class MirrorProfile:
    """
    Impairments applied by one mirror.

    All rates are per *request* unless stated otherwise. A rate of 0.0 disables
    the impairment entirely, and a profile with every field at its default is a
    perfect mirror.
    """

    name: str = "clean"

    # Steady-state behaviour
    bandwidth_bps: Optional[float] = None
    """
    Write pacing in bytes/sec, applied **per connection**, not as a shared cap.

    This is a modelling decision and it matters, because it is the condition
    under which parallel download helps at all. N connections to a mirror
    profiled at B bytes/sec therefore see up to N*B in aggregate.

    Per-connection throttling is the common case in practice -- CDNs, object
    stores and most origins limit each connection rather than each client -- and
    it is why parallel downloaders exist. But a path limited by a shared
    bottleneck link behaves differently: there, extra connections divide the same
    capacity and parallelism buys nothing beyond hiding per-request latency. Any
    speedup measured here is a speedup against a per-connection limit, and should
    be reported as such rather than as a general result.
    """

    latency_seconds: float = 0.0
    """Delay before the first byte of a response, standing in for RTT."""

    jitter_seconds: float = 0.0
    """Uniform +/- jitter added to latency_seconds."""

    # Faults
    failure_rate: float = 0.0
    """Fraction of ranged requests answered with 503."""

    corruption_rate: float = 0.0
    """
    Fraction of ranged requests in which one byte is silently flipped.

    This is the impairment the integrity path exists for: the response is
    well-formed, the length is right, and only the content is wrong.
    """

    truncation_rate: float = 0.0
    """Fraction of ranged requests that close the connection mid-body."""

    # Non-stationarity
    degrade_after_bytes: Optional[int] = None
    """Once this many bytes have been served, switch to ``degraded``."""

    degraded: Optional["MirrorProfile"] = None
    """Profile adopted after ``degrade_after_bytes``. Enables failover scenarios."""

    def resolve(self, bytes_served: int) -> "MirrorProfile":
        """The profile in force after this many bytes."""
        if (
            self.degrade_after_bytes is not None
            and self.degraded is not None
            and bytes_served >= self.degrade_after_bytes
        ):
            return self.degraded
        return self


@dataclass
class MirrorStats:
    """What a mirror observed. These are the measurements, not the client's view."""

    requests: int = 0
    ranged_requests: int = 0
    bytes_written: int = 0
    """Bytes actually written to sockets, including bytes the client discarded."""
    failures_injected: int = 0
    corruptions_injected: int = 0
    truncations_injected: int = 0
    corrupted_offsets: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "ranged_requests": self.ranged_requests,
            "bytes_written": self.bytes_written,
            "failures_injected": self.failures_injected,
            "corruptions_injected": self.corruptions_injected,
            "truncations_injected": self.truncations_injected,
        }


def _draw(seed: int, *key: int) -> float:
    """
    Deterministic uniform draw in [0, 1) from a seed and an integer key.

    Keyed rather than sequential so an impairment depends only on *which* bytes
    are being requested, never on the order requests happen to arrive in. Two
    schedulers fetching the same file therefore meet the same faults.
    """
    payload = struct.pack(">q" + "q" * len(key), seed, *key)
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return struct.unpack(">Q", digest)[0] / float(1 << 64)


class FaultyMirror(ThreadingHTTPServer):
    """An HTTP origin serving byte ranges under a MirrorProfile."""

    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True

    def __init__(
        self,
        payload: bytes,
        profile: Optional[MirrorProfile] = None,
        seed: int = 0,
        etag: str = '"v1"',
    ) -> None:
        super().__init__(("127.0.0.1", 0), _FaultyHandler)
        self.payload = payload
        self.profile = profile or MirrorProfile()
        self.seed = seed
        self.etag = etag
        self.stats = MirrorStats()
        self.lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/payload.bin"

    def start(self) -> "FaultyMirror":
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def __enter__(self) -> "FaultyMirror":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        # Clients abandon streams deliberately in these experiments.
        pass


class _FaultyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FaultyMirror

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    # ------------------------------------------------------------------ helpers

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        base = {"Accept-Ranges": "bytes", "ETag": self.server.etag}
        base.update(extra or {})
        return base

    def _respond(self, status: int, body: bytes, head: bool, extra: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._headers(extra).items():
            self.send_header(key, value)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def _paced_write(self, body: bytes, bandwidth_bps: Optional[float]) -> int:
        """
        Write the body, pacing it to the configured bandwidth.

        Returns bytes actually written. Pacing is done in 32 KB slices so a slow
        mirror is slow throughout rather than delayed and then instant, which is
        what makes a straggler look like a straggler to the client.
        """
        if not bandwidth_bps or bandwidth_bps <= 0:
            self.wfile.write(body)
            return len(body)

        slice_size = 32 * 1024
        written = 0
        for start in range(0, len(body), slice_size):
            piece = body[start:start + slice_size]
            self.wfile.write(piece)
            self.wfile.flush()
            written += len(piece)
            time.sleep(len(piece) / bandwidth_bps)
        return written

    # ------------------------------------------------------------------- serving

    def _serve(self, head: bool) -> None:
        srv = self.server
        with srv.lock:
            srv.stats.requests += 1
            served_so_far = srv.stats.bytes_written
        profile = srv.profile.resolve(served_so_far)

        range_header = self.headers.get("Range")
        if_range = self.headers.get("If-Range")

        if not range_header or (if_range and if_range != srv.etag):
            # A whole-file GET is shaped exactly like a ranged one. It previously
            # bypassed pacing and latency entirely, which made any single-stream
            # tool appear roughly ten times faster than the link allowed and
            # rendered every baseline comparison meaningless.
            if profile.latency_seconds > 0:
                time.sleep(profile.latency_seconds)
            self.send_response(200)
            self.send_header("Content-Length", str(len(srv.payload)))
            for key, value in self._headers().items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                try:
                    written = self._paced_write(srv.payload, profile.bandwidth_bps)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    written = len(srv.payload)
                with srv.lock:
                    srv.stats.bytes_written += written
            return

        start_s, _, end_s = range_header.split("=", 1)[1].partition("-")
        start = int(start_s)
        end = min(int(end_s), len(srv.payload) - 1) if end_s else len(srv.payload) - 1
        with srv.lock:
            srv.stats.ranged_requests += 1
            attempt = srv.stats.ranged_requests

        delay = profile.latency_seconds
        if profile.jitter_seconds:
            delay += (_draw(srv.seed, start, 1) * 2 - 1) * profile.jitter_seconds
        if delay > 0:
            time.sleep(max(0.0, delay))

        # Failure: keyed on the range *and* the attempt count, so a retry of the
        # same range is not doomed to fail forever at a non-zero failure rate.
        if profile.failure_rate and _draw(srv.seed, start, 2, attempt) < profile.failure_rate:
            with srv.lock:
                srv.stats.failures_injected += 1
            self._respond(503, b"", head, {})
            return

        body = bytearray(srv.payload[start:end + 1])

        if profile.corruption_rate and _draw(srv.seed, start, 3, attempt) < profile.corruption_rate:
            if body:
                position = int(_draw(srv.seed, start, 4, attempt) * len(body))
                body[position] ^= 0xFF
                with srv.lock:
                    srv.stats.corruptions_injected += 1
                    srv.stats.corrupted_offsets.append(start + position)

        truncate = bool(
            profile.truncation_rate
            and _draw(srv.seed, start, 5, attempt) < profile.truncation_rate
        )

        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._headers(
            {"Content-Range": f"bytes {start}-{end}/{len(srv.payload)}"}
        ).items():
            self.send_header(key, value)
        self.end_headers()

        if head:
            return

        payload = bytes(body)
        if truncate:
            # Declare the full length, send half, drop the connection. This is the
            # shape of a mid-transfer reset, which the client must detect as a
            # short read rather than accept as a complete chunk.
            payload = payload[: len(payload) // 2]
            with srv.lock:
                srv.stats.truncations_injected += 1

        try:
            written = self._paced_write(payload, profile.bandwidth_bps)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The client abandoned the stream; count only what reached the socket
            # buffer. Early abort is a legitimate client strategy here and the
            # bytes it avoided should not be charged to it.
            written = len(payload)

        with srv.lock:
            srv.stats.bytes_written += written

        if truncate:
            self.close_connection = True

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
