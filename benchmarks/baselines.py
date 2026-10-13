"""
Run other download tools against the same mirrors, for comparison.

A parallel downloader that reports no numbers for aria2 is not making a claim a
reviewer can check, so this exists to make that comparison runnable. Tools that
are not installed are reported as missing rather than silently skipped: "aria2c
was not available" and "aria2c was not faster" are very different statements and
the output must not blur them.

Fairness notes, because the defaults are misleading
---------------------------------------------------
* **aria2c** opens one connection per *server* unless `--split` says otherwise.
  Passing `-x N` alone does not give N connections to a single origin, and
  benchmarks that omit `--split` make aria2 look far worse than it is. Both are
  set here, and the exact invocation is recorded with every result.
* **aria2c** also applies `--min-split-size` (20 MB by default), which silently
  caps the real split count on smaller files. It is set explicitly.
* **curl** and **wget** are single-stream references, not competitors. They
  establish what one connection achieves on the same path, which is the number
  that makes a parallel speedup meaningful.
* Every tool writes to a fresh file in a fresh directory, and the output is
  hashed afterwards, so a tool that silently truncates is caught rather than
  credited with a fast run.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

MB = 1024 * 1024


@dataclass
class ToolResult:
    """One external tool's attempt at one transfer."""

    tool: str
    available: bool
    command: list[str] = field(default_factory=list)
    ok: bool = False
    verified: bool = False
    seconds: float = 0.0
    goodput_bps: float = 0.0
    bytes_written: int = 0
    """Bytes the mirrors wrote, measured server-side as for ReliaDL."""
    wasted_bytes: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Tool:
    """How to invoke one downloader."""

    name: str
    executable: str
    parallel: bool

    def command(self, url: str, output: Path, workers: int, chunk_size: int) -> list[str]:
        if self.name == "aria2c":
            return [
                self.executable,
                f"--max-connection-per-server={workers}",
                # Without --split, -x alone does not open N connections.
                f"--split={workers}",
                # Default is 20M, which silently caps the split on smaller files.
                f"--min-split-size={max(1, chunk_size // MB)}M",
                "--file-allocation=none",
                "--check-certificate=false",
                "--summary-interval=0",
                "--console-log-level=error",
                f"--dir={output.parent}",
                f"--out={output.name}",
                url,
            ]
        if self.name == "curl":
            return [self.executable, "-sS", "--fail", "-o", str(output), url]
        if self.name == "wget":
            return [self.executable, "-q", "-O", str(output), url]
        if self.name == "axel":
            return [self.executable, "-q", "-n", str(workers), "-o", str(output), url]
        raise ValueError(f"no invocation defined for {self.name}")


TOOLS: tuple[Tool, ...] = (
    Tool("aria2c", "aria2c", parallel=True),
    Tool("axel", "axel", parallel=True),
    Tool("curl", "curl", parallel=False),
    Tool("wget", "wget", parallel=False),
)


def available_tools() -> dict[str, Optional[str]]:
    """Map each tool name to its resolved path, or None when not installed."""
    return {tool.name: shutil.which(tool.executable) for tool in TOOLS}


def run_tool(
    tool: Tool,
    url: str,
    expected_sha256: str,
    file_size: int,
    *,
    workers: int = 4,
    chunk_size: int = 4 * MB,
    timeout: float = 300.0,
    mirrors_bytes_written: Optional[callable] = None,
) -> ToolResult:
    """
    Run one tool once and measure it.

    ``mirrors_bytes_written`` is a callable returning the servers' total written
    bytes, so waste is measured the same way it is for ReliaDL rather than being
    inferred from the client.
    """
    path = shutil.which(tool.executable)
    if path is None:
        return ToolResult(tool=tool.name, available=False, error="not installed")

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "download.bin"
        command = tool.command(url, output, workers, chunk_size)
        before = mirrors_bytes_written() if mirrors_bytes_written else 0

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool=tool.name, available=True, command=command,
                seconds=time.perf_counter() - started, error="timeout",
            )
        elapsed = time.perf_counter() - started

        result = ToolResult(
            tool=tool.name, available=True, command=command, seconds=elapsed
        )
        if completed.returncode != 0:
            result.error = (completed.stderr or completed.stdout or "").strip()[:300]
            return result

        if not output.is_file():
            result.error = "tool exited 0 but wrote no file"
            return result

        data = output.read_bytes()
        result.ok = True
        # Hashed rather than trusted: a tool that truncates silently must not be
        # credited with a fast transfer.
        result.verified = hashlib.sha256(data).hexdigest() == expected_sha256
        result.goodput_bps = len(data) / elapsed if elapsed else 0.0
        if mirrors_bytes_written:
            result.bytes_written = mirrors_bytes_written() - before
            result.wasted_bytes = max(0, result.bytes_written - file_size)
        if not result.verified:
            result.error = f"hash mismatch ({len(data)} of {file_size} bytes)"
        return result


def run_all(
    url: str,
    expected_sha256: str,
    file_size: int,
    *,
    workers: int = 4,
    chunk_size: int = 4 * MB,
    timeout: float = 300.0,
    mirrors_bytes_written: Optional[callable] = None,
    only: Optional[Sequence[str]] = None,
) -> list[ToolResult]:
    """Run every known tool once, including the ones that are missing."""
    results = []
    for tool in TOOLS:
        if only and tool.name not in only:
            continue
        results.append(
            run_tool(
                tool, url, expected_sha256, file_size,
                workers=workers, chunk_size=chunk_size, timeout=timeout,
                mirrors_bytes_written=mirrors_bytes_written,
            )
        )
    return results
