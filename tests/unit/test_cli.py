"""
Unit tests for ReliaDL Command Line Interface (CLI) subcommands.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from reliadl.cli import (
    build_parser,
    main,
    run_benchmark,
    run_doctor,
    run_hash_tree,
    run_inspect_state,
    run_probe,
    run_top,
    run_verify,
)
from reliadl.hash_verifier import compute_file_hash
from reliadl.manifest import ChunkManifest, ManifestChunk, ArtifactMetadata, ChunkingTopology


class TestCLISubcommands(unittest.TestCase):
    """Test suite covering all ReliaDL CLI subcommands."""

    def test_parser_construction(self) -> None:
        """Test build_parser and subcommand registration."""
        parser = build_parser()
        args = parser.parse_args(["benchmark", "--duration", "2.0"])
        self.assertEqual(args.command, "benchmark")
        self.assertEqual(args.duration, 2.0)

    def test_benchmark_command(self) -> None:
        """Test run_benchmark execution."""
        parser = build_parser()
        args = parser.parse_args(["benchmark", "--duration", "1.0", "--block-size", "16"])
        ret = run_benchmark(args)
        self.assertEqual(ret, 0)

    def test_doctor_command(self) -> None:
        """Test run_doctor environment audit."""
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        ret = run_doctor(args)
        self.assertEqual(ret, 0)

    def test_probe_command_local(self) -> None:
        """Test run_probe against mock endpoint."""
        parser = build_parser()
        args = parser.parse_args(["probe", "--url", "https://httpbin.org/get", "--timeout", "2.0"])
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.headers = {"Accept-Ranges": "bytes", "Content-Length": "1024"}
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            ret = run_probe(args)
            self.assertEqual(ret, 0)

    def test_inspect_state_manifest(self) -> None:
        """Test run_inspect_state on ChunkGuard manifest."""
        manifest = ChunkManifest(
            manifest_version="1.0.0",
            artifact_metadata=ArtifactMetadata(
                filename="test.tar.gz",
                file_size_bytes=1048576,
                file_hash_sha256="a" * 64,
            ),
            chunking_topology=ChunkingTopology(
                default_chunk_size_bytes=1048576,
                total_chunks=1,
                hash_algorithm="sha256",
            ),
            chunks=[
                ManifestChunk(
                    index=0,
                    start_byte=0,
                    end_byte=1048575,
                    size_bytes=1048576,
                    sha256="b" * 64,
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "test.cgmanifest"
            manifest.save(manifest_path)

            parser = build_parser()
            args = parser.parse_args(["inspect-state", "--state-file", str(manifest_path)])
            ret = run_inspect_state(args)
            self.assertEqual(ret, 0)

            args_json = parser.parse_args(["inspect-state", "--state-file", str(manifest_path), "--json"])
            ret_json = run_inspect_state(args_json)
            self.assertEqual(ret_json, 0)

    def test_top_command(self) -> None:
        """Test run_top telemetry dashboard command."""
        parser = build_parser()
        args = parser.parse_args(["top", "--metrics-port", "9090", "--once"])
        ret = run_top(args)
        self.assertEqual(ret, 0)

    def test_hash_tree_command(self) -> None:
        """Test run_hash_tree segment Merkle root audit command."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"Hello ReliaDL Merkle Audit Engine!\n" * 100)
            tmp_path = Path(tmp.name)

        try:
            parser = build_parser()
            args = parser.parse_args(["hash-tree", "--file", str(tmp_path), "--block-size", "512"])
            ret = run_hash_tree(args)
            self.assertEqual(ret, 0)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_verify_command(self) -> None:
        """Test run_verify hash matching."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"Reliable downloads everywhere.")
            tmp_path = Path(tmp.name)

        try:
            expected = compute_file_hash(tmp_path, algorithm="sha256")
            parser = build_parser()
            args = parser.parse_args(["verify", "--file", str(tmp_path), "--expected-hash", expected])
            ret = run_verify(args)
            self.assertEqual(ret, 0)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def test_main_entrypoint(self) -> None:
        """Test main entrypoint with --version and help."""
        with self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
