"""
Unit tests for ReliaDL Command Line Interface (CLI) subcommands.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from reliadl.cli import (
    _engine_config,
    build_parser,
    main,
    run_benchmark,
    run_doctor,
    run_hash_tree,
    run_inspect_state,
    run_manifest,
    run_probe,
    run_top,
    run_verify,
)
from reliadl.hash_verifier import compute_file_hash
from reliadl.manifest import (
    ArtifactMetadata,
    ChunkManifest,
    ChunkingTopology,
    ManifestChunk,
    load_manifest,
)


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

    def test_inspect_state_reads_a_download_state_file(self) -> None:
        """
        The state-file branch of inspect-state, which was entirely broken.

        It called StateManager.snapshot(), which does not exist, so the command
        failed every time it was pointed at an actual .state file. Only the
        manifest branch was covered, and a broad `except Exception` reported the
        AttributeError as a parse failure, so nothing looked wrong. This asserts
        on the reported contents, not merely on the exit code, because the old
        code "succeeded" at printing an error.
        """
        import json
        from io import StringIO
        from contextlib import redirect_stdout

        from reliadl.models import ChunkState, DownloadState, DownloadStatus
        from reliadl.state_manager import StateManager

        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "payload.bin"
            chunks = [
                ChunkState(index=0, start_byte=0, end_byte=511),
                ChunkState(index=1, start_byte=512, end_byte=1023),
            ]
            chunks[0].mark_complete("c" * 64)
            state = DownloadState(
                download_id="d" * 8,
                url="http://example.com/payload.bin",
                target_path=str(target),
                file_size=1024,
                chunk_size=512,
                hash_algorithm="sha256",
                status=DownloadStatus.DOWNLOADING,
                chunks=chunks,
            )
            state_path = Path(tmpdir) / "payload.bin.state"
            StateManager().save(state, state_path)

            parser = build_parser()
            args = parser.parse_args(
                ["inspect-state", "--state-file", str(state_path), "--json"]
            )
            buffer = StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(run_inspect_state(args), 0)

            report = json.loads(buffer.getvalue())
            self.assertEqual(report["file_size"], 1024)
            self.assertEqual(report["total_chunks"], 2)
            self.assertEqual(report["completed_chunks"], 1)
            self.assertEqual(report["pending_chunks"], 1)
            self.assertEqual(report["bytes_downloaded"], 512)
            self.assertEqual(report["url"], "http://example.com/payload.bin")
            self.assertEqual(report["status"], "DOWNLOADING")

            text_args = parser.parse_args(
                ["inspect-state", "--state-file", str(state_path)]
            )
            text_buffer = StringIO()
            with redirect_stdout(text_buffer):
                self.assertEqual(run_inspect_state(text_args), 0)
            printed = text_buffer.getvalue()
            self.assertIn("Completed Chunks : 1 / 2", printed)
            self.assertNotIn("[ERROR]", printed)

    def test_inspect_state_reports_a_missing_file_as_failure(self) -> None:
        """A path that is neither a manifest nor a state file must exit non-zero."""
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "absent.state"
            parser = build_parser()
            args = parser.parse_args(["inspect-state", "--state-file", str(missing)])
            self.assertEqual(run_inspect_state(args), 1)

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


class TestManifestCommands(unittest.TestCase):
    """
    The `manifest` command group and the `--manifest` download flag.

    These exist because the engine's sub-block localization -- the mechanism
    behind the project's headline completion result -- was reachable only from
    the Python API: there was no way to build a manifest from the CLI, no
    --manifest flag on `download`, and `get_download_config` never mapped
    `manifest_path` out of the download section. Each test below pins one link
    in that chain.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.payload = self.work / "payload.bin"
        # Two full 1 MB chunks plus a short tail, so the last-chunk path is covered.
        self.payload.write_bytes(bytes(range(256)) * 8192 + b"tail")
        self.addCleanup(self._tmp.cleanup)

    def _generate(self, *extra: str) -> Path:
        out = self.work / "payload.cgmanifest"
        args = build_parser().parse_args(
            ["manifest", "generate", "--file", str(self.payload),
             "-o", str(out), "--chunk-size", "1MB", *extra]
        )
        self.assertEqual(run_manifest(args), 0)
        return out

    def test_generate_produces_loadable_manifest_with_sub_blocks(self) -> None:
        manifest = load_manifest(self._generate())
        self.assertTrue(manifest.verify_merkle_root())
        self.assertEqual(
            manifest.artifact_metadata.file_hash_sha256,
            compute_file_hash(self.payload, algorithm="sha256"),
        )
        self.assertEqual(
            sum(c.size_bytes for c in manifest.chunks), self.payload.stat().st_size
        )
        # Sub-block digests are the whole point: without them the engine can only
        # discard and re-fetch a whole chunk.
        self.assertTrue(all(c.sub_blocks for c in manifest.chunks))

    def test_no_sub_blocks_flag_omits_digests(self) -> None:
        manifest = load_manifest(self._generate("--no-sub-blocks"))
        self.assertTrue(all(not c.sub_blocks for c in manifest.chunks))

    def test_chunks_are_contiguous_and_non_overlapping(self) -> None:
        chunks = load_manifest(self._generate()).chunks
        self.assertEqual(chunks[0].start_byte, 0)
        for previous, nxt in zip(chunks, chunks[1:]):
            self.assertEqual(nxt.start_byte, previous.end_byte + 1)
        self.assertEqual(chunks[-1].end_byte, self.payload.stat().st_size - 1)

    def test_keygen_sign_and_verify_round_trip(self) -> None:
        manifest_path = self._generate()
        key = self.work / "key.pem"
        parser = build_parser()

        self.assertEqual(
            run_manifest(parser.parse_args(["manifest", "keygen", "-o", str(key)])), 0
        )
        self.assertTrue(key.exists() and key.with_suffix(".pem.pub").exists())

        self.assertEqual(
            run_manifest(parser.parse_args(
                ["manifest", "sign", "--manifest", str(manifest_path),
                 "--key", str(key), "--key-id", "test-key"]
            )),
            0,
        )
        self.assertTrue(load_manifest(manifest_path).is_signed)

        self.assertEqual(
            run_manifest(parser.parse_args(
                ["manifest", "verify", "--manifest", str(manifest_path),
                 "--public-key", str(key.with_suffix(".pem.pub")),
                 "--file", str(self.payload)]
            )),
            0,
        )

    def test_verify_fails_on_tampered_artifact(self) -> None:
        manifest_path = self._generate()
        tampered = self.work / "tampered.bin"
        data = bytearray(self.payload.read_bytes())
        data[1000] ^= 0xFF
        tampered.write_bytes(bytes(data))
        args = build_parser().parse_args(
            ["manifest", "verify", "--manifest", str(manifest_path), "--file", str(tampered)]
        )
        self.assertEqual(run_manifest(args), 1)

    def test_verify_fails_when_chunk_list_contradicts_root(self) -> None:
        manifest_path = self._generate()
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["chunks"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        args = build_parser().parse_args(
            ["manifest", "verify", "--manifest", str(manifest_path)]
        )
        self.assertEqual(run_manifest(args), 1)

    def test_download_manifest_flag_reaches_the_engine_config(self) -> None:
        """
        The regression this guards: the flag parsed, the override was written
        into the download section, and `get_download_config` dropped it on the
        floor, so --manifest was accepted and silently ignored.
        """
        manifest_path = self._generate()
        args = build_parser().parse_args(
            ["download", "--url", "http://example.invalid/f.bin", "-o", str(self.work / "out.bin"),
             "--manifest", str(manifest_path)]
        )
        self.assertEqual(_engine_config(args).manifest_path, str(manifest_path))

    def test_download_without_manifest_leaves_path_unset(self) -> None:
        args = build_parser().parse_args(
            ["download", "--url", "http://example.invalid/f.bin", "-o", str(self.work / "out.bin")]
        )
        self.assertIsNone(_engine_config(args).manifest_path)


if __name__ == "__main__":
    unittest.main()
