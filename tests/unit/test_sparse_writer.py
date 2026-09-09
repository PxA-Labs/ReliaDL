"""
Unit tests for SparseFileWriter: zero-assembly direct sparse file pre-allocation
and concurrent positional writes.
"""

from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.exceptions import (
    AllocationError,
    DiskFullError,
    FileHashMismatchError,
    StorageError,
    StoragePermissionError,
)
from src.models import ChunkSpec
from src.sparse_writer import (
    AllocationStrategy,
    SparseFileWriter,
    _normalize_hex_hash,
)


class TestSparseFileWriter(unittest.TestCase):
    """Test suite for SparseFileWriter zero-assembly direct storage."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="reliadl_sparse_test_")
        self.base_path = Path(self.temp_dir).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_preallocation_auto_and_sparse(self) -> None:
        """Test that file is initialized and pre-allocated to the exact total size."""
        file_path = self.base_path / "target_sparse.bin"
        total_size = 128 * 1024  # 128 KB

        with SparseFileWriter(file_path, total_size=total_size, strategy=AllocationStrategy.SPARSE) as writer:
            self.assertEqual(writer.target_path, file_path)
            self.assertEqual(writer.total_size, total_size)
            self.assertEqual(writer.current_size, total_size)
            self.assertTrue(writer.is_preallocated)
            self.assertFalse(writer.is_closed)
            self.assertEqual(writer.peak_overhead_ratio, 1.0)

        self.assertTrue(file_path.exists())
        self.assertEqual(file_path.stat().st_size, total_size)

    def test_zero_byte_file(self) -> None:
        """Test handling of 0-byte destination files."""
        file_path = self.base_path / "empty.bin"
        with SparseFileWriter(file_path, total_size=0) as writer:
            self.assertEqual(writer.current_size, 0)
            self.assertEqual(writer.write_at(0, b""), 0)
            self.assertEqual(writer.read_at(0, 0), b"")
            self.assertTrue(writer.verify_hash(hashlib.sha256(b"").hexdigest()))

        self.assertEqual(file_path.stat().st_size, 0)

    def test_negative_total_size_raises_value_error(self) -> None:
        """Test that negative total_size is rejected."""
        file_path = self.base_path / "invalid.bin"
        with self.assertRaises(ValueError):
            SparseFileWriter(file_path, total_size=-1)

    def test_out_of_order_positional_writes_and_reads(self) -> None:
        """Test non-sequential positional writes into pre-allocated file."""
        file_path = self.base_path / "out_of_order.bin"
        total_size = 300
        part1 = b"A" * 100
        part2 = b"B" * 100
        part3 = b"C" * 100

        with SparseFileWriter(file_path, total_size=total_size) as writer:
            # Write out of order: part3 (offset 200), then part1 (offset 0), then part2 (offset 100)
            writer.write_at(200, part3)
            writer.write_at(0, part1)
            writer.write_at(100, part2)

            # Verify reads at exact offsets
            self.assertEqual(writer.read_at(0, 100), part1)
            self.assertEqual(writer.read_at(100, 100), part2)
            self.assertEqual(writer.read_at(200, 100), part3)
            self.assertEqual(writer.read_at(50, 100), b"A" * 50 + b"B" * 50)

        # Read back whole file from disk
        with open(file_path, "rb") as f:
            content = f.read()
        self.assertEqual(content, part1 + part2 + part3)

    def test_concurrent_positional_writes_zero_overlap_corruption(self) -> None:
        """
        Test that multi-threaded concurrent positional writes of non-overlapping chunks
        result in zero byte corruption and exact payload match.
        """
        file_path = self.base_path / "concurrent_assembly.bin"
        num_chunks = 32
        chunk_size = 8 * 1024  # 8 KB per chunk
        total_size = num_chunks * chunk_size

        # Create reference binary data with pseudo-random content
        rng = random.Random(42)
        full_data = rng.randbytes(total_size)
        expected_hash = hashlib.sha256(full_data).hexdigest()

        # Slice data into chunk specs
        chunks: list[tuple[ChunkSpec, bytes]] = []
        for i in range(num_chunks):
            start = i * chunk_size
            end = start + chunk_size - 1
            spec = ChunkSpec(index=i, start_byte=start, end_byte=end, size=chunk_size)
            chunk_bytes = full_data[start : end + 1]
            chunks.append((spec, chunk_bytes))

        # Shuffle chunk order to simulate real-world asynchronous download arrivals
        rng.shuffle(chunks)

        with SparseFileWriter(file_path, total_size=total_size) as writer:
            def _write_task(spec: ChunkSpec, payload: bytes) -> int:
                return writer.write_chunk(spec, payload)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(_write_task, spec, payload) for spec, payload in chunks]
                for future in concurrent.futures.as_completed(futures):
                    written = future.result()
                    self.assertEqual(written, chunk_size)

            # Verify integrity via writer
            self.assertTrue(writer.verify_hash(expected_hash))

        # Re-verify from disk directly
        with open(file_path, "rb") as f:
            disk_content = f.read()

        self.assertEqual(len(disk_content), total_size)
        self.assertEqual(disk_content, full_data)
        self.assertEqual(hashlib.sha256(disk_content).hexdigest(), expected_hash)

    def test_peak_disk_overhead_ratio(self) -> None:
        """Verify that peak disk overhead is exactly 1.0x and no intermediate chunks are created."""
        file_path = self.base_path / "overhead_test.bin"
        total_size = 64 * 1024

        with SparseFileWriter(file_path, total_size=total_size) as writer:
            self.assertEqual(writer.peak_overhead_ratio, 1.0)
            writer.write_at(0, b"X" * (32 * 1024))
            writer.write_at(32 * 1024, b"Y" * (32 * 1024))

            # Directory should only contain the destination file
            files_in_dir = list(self.base_path.iterdir())
            self.assertEqual(len(files_in_dir), 1)
            self.assertEqual(files_in_dir[0].resolve(), file_path.resolve())

    def test_write_chunk_validation(self) -> None:
        """Test write_chunk with ChunkSpec and integer index."""
        file_path = self.base_path / "chunk_spec_test.bin"
        total_size = 200

        with SparseFileWriter(file_path, total_size=total_size) as writer:
            spec = ChunkSpec(index=0, start_byte=0, end_byte=99, size=100)
            # Valid write
            written = writer.write_chunk(spec, b"1" * 100)
            self.assertEqual(written, 100)

            # Mismatched payload size raises StorageError
            with self.assertRaises(StorageError):
                writer.write_chunk(spec, b"1" * 50)

            # Integer index without offset raises ValueError
            with self.assertRaises(ValueError):
                writer.write_chunk(1, b"2" * 100)

            # Integer index with offset succeeds
            written2 = writer.write_chunk(1, b"2" * 100, offset=100)
            self.assertEqual(written2, 100)

    def test_write_out_of_bounds_raises_storage_error(self) -> None:
        """Test writing beyond total_size or negative offset raises StorageError."""
        file_path = self.base_path / "bounds_test.bin"
        total_size = 100

        with SparseFileWriter(file_path, total_size=total_size) as writer:
            with self.assertRaises(StorageError):
                writer.write_at(-1, b"data")

            with self.assertRaises(StorageError):
                writer.write_at(80, b"A" * 30)  # 80 + 30 = 110 > 100

            with self.assertRaises(StorageError):
                writer.read_at(-5, 10)

    def test_operations_on_closed_writer_raise_storage_error(self) -> None:
        """Test that writing, reading, or syncing on a closed writer raises StorageError."""
        file_path = self.base_path / "closed_test.bin"
        writer = SparseFileWriter(file_path, total_size=100)
        writer.close()
        self.assertTrue(writer.is_closed)

        with self.assertRaises(StorageError):
            writer.write_at(0, b"abc")

        with self.assertRaises(StorageError):
            writer.read_at(0, 10)

        with self.assertRaises(StorageError):
            writer.sync()

        with self.assertRaises(StorageError):
            _ = writer.current_size

    def test_verify_hash_mismatch_raises_file_hash_mismatch_error(self) -> None:
        """Test that verify_hash raises FileHashMismatchError on mismatch."""
        file_path = self.base_path / "hash_mismatch.bin"
        content = b"expected reliable content"
        with SparseFileWriter(file_path, total_size=len(content)) as writer:
            writer.write_at(0, content)
            # Correct hash succeeds
            expected_hash = hashlib.sha256(content).hexdigest()
            self.assertTrue(writer.verify_hash(expected_hash))
            self.assertTrue(writer.verify_hash(f"sha256:{expected_hash}"))

            # Tampered / wrong hash fails
            wrong_hash = "0" * 64
            with self.assertRaises(FileHashMismatchError) as ctx:
                writer.verify_hash(wrong_hash)
            self.assertEqual(ctx.exception.expected_hash, wrong_hash)
            self.assertEqual(ctx.exception.computed_hash, expected_hash)

    def test_disk_full_error_preallocation(self) -> None:
        """Test that insufficient disk space triggers DiskFullError."""
        file_path = self.base_path / "disk_full.bin"
        fake_usage = MagicMock(free=500, total=1000, used=500)

        with patch("shutil.disk_usage", return_value=fake_usage):
            with self.assertRaises(DiskFullError) as ctx:
                SparseFileWriter(file_path, total_size=10000, check_disk_space=True)
            self.assertEqual(ctx.exception.available_bytes, 500)
            self.assertEqual(ctx.exception.required_bytes, 10000)

    def test_allocation_error_propagation(self) -> None:
        """Test that system allocation errors raise AllocationError."""
        file_path = self.base_path / "alloc_error.bin"
        with patch("os.ftruncate", side_effect=OSError(errno.EIO, "I/O failure")):
            with self.assertRaises(AllocationError):
                SparseFileWriter(file_path, total_size=1024, strategy=AllocationStrategy.SPARSE)

    def test_normalize_hex_hash(self) -> None:
        """Test stripping algorithmic prefixes and whitespace from hash strings."""
        self.assertEqual(_normalize_hex_hash("  ABCD123  "), "abcd123")
        self.assertEqual(_normalize_hex_hash("sha256:abcdef"), "abcdef")
        self.assertEqual(_normalize_hex_hash("SHA512:123456"), "123456")


if __name__ == "__main__":
    unittest.main()
