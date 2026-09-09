"""
Unit tests for .cgmanifest JSON schema parser, validator, and model bindings.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.exceptions import ManifestError, ManifestFormatError
from src.manifest import (
    ArtifactMetadata,
    ChunkingTopology,
    ChunkManifest,
    ManifestChunk,
    ManifestGenerator,
    ManifestSignature,
    MirrorSpec,
    dump_manifest,
    load_manifest,
    parse_manifest_json,
    validate_manifest_dict,
)
from src.models import ChunkSpec


def _make_sample_manifest_dict() -> dict:
    """Generate a valid manifest dictionary fixture for testing."""
    return {
        "manifest_version": "1.0.0",
        "generator": {
            "name": "ReliaDL Test CLI",
            "version": "1.0.0",
            "timestamp": "2026-09-08T12:00:00Z",
        },
        "artifact_metadata": {
            "filename": "ubuntu-24.04-server.iso",
            "file_size_bytes": 2097152,
            "file_hash_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "file_hash_sha512": "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e",
            "content_type": "application/x-iso9660-image",
            "custom_metadata": {"release_channel": "lts"},
        },
        "mirrors": [
            {
                "url": "https://mirror1.example.com/ubuntu.iso",
                "region": "us-east-1",
                "priority": 1,
            },
            {
                "url": "https://mirror2.example.com/ubuntu.iso",
                "region": "eu-west-1",
                "priority": 2,
            },
        ],
        "chunking_topology": {
            "default_chunk_size_bytes": 1048576,
            "total_chunks": 2,
            "hash_algorithm": "sha256",
            "merkle_tree_root": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        },
        "chunks": [
            {
                "index": 0,
                "start_byte": 0,
                "end_byte": 1048575,
                "size_bytes": 1048576,
                "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "sub_blocks": [
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ],
            },
            {
                "index": 1,
                "start_byte": 1048576,
                "end_byte": 2097151,
                "size_bytes": 1048576,
                "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ],
        "signature": {
            "algorithm": "ed25519",
            "key_id": "key_release_2026",
            "public_key": "dGVzdF9wdWJsaWNfa2V5",
            "signature_hex": "deadbeef1234",
        },
    }


class TestManifestSchema(unittest.TestCase):
    """Test suite for .cgmanifest schema validation, parsing, and model bindings."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="reliadl_manifest_test_")
        self.base_path = Path(self.temp_dir).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_valid_manifest_parsing_and_model_bindings(self) -> None:
        """Test parsing valid manifest fixture into domain models."""
        raw_dict = _make_sample_manifest_dict()
        raw_json = json.dumps(raw_dict)

        manifest = parse_manifest_json(raw_json)

        self.assertEqual(manifest.manifest_version, "1.0.0")
        self.assertIsNotNone(manifest.generator)
        self.assertEqual(manifest.generator.name, "ReliaDL Test CLI")

        # Metadata
        self.assertEqual(manifest.artifact_metadata.filename, "ubuntu-24.04-server.iso")
        self.assertEqual(manifest.artifact_metadata.file_size_bytes, 2097152)
        self.assertEqual(len(manifest.artifact_metadata.file_hash_sha256), 64)

        # Mirrors
        self.assertEqual(len(manifest.mirrors), 2)
        self.assertEqual(manifest.mirrors[0].url, "https://mirror1.example.com/ubuntu.iso")
        self.assertEqual(manifest.mirrors[0].priority, 1)

        # Topology
        self.assertEqual(manifest.chunking_topology.default_chunk_size_bytes, 1048576)
        self.assertEqual(manifest.chunking_topology.total_chunks, 2)
        self.assertEqual(manifest.chunking_topology.hash_algorithm, "sha256")

        # Chunks
        self.assertEqual(len(manifest.chunks), 2)
        self.assertEqual(manifest.chunks[0].index, 0)
        self.assertEqual(manifest.chunks[0].size_bytes, 1048576)
        self.assertEqual(manifest.chunks[1].index, 1)

        # Signature
        self.assertIsNotNone(manifest.signature)
        self.assertEqual(manifest.signature.algorithm, "ed25519")

    def test_chunk_spec_conversion(self) -> None:
        """Test converting ManifestChunk and ChunkManifest to ChunkSpec domain models."""
        manifest = parse_manifest_json(json.dumps(_make_sample_manifest_dict()))
        specs = manifest.get_chunk_specs()

        self.assertEqual(len(specs), 2)
        self.assertIsInstance(specs[0], ChunkSpec)
        self.assertEqual(specs[0].index, 0)
        self.assertEqual(specs[0].start_byte, 0)
        self.assertEqual(specs[0].end_byte, 1048575)
        self.assertEqual(specs[0].size, 1048576)

        self.assertEqual(specs[1].index, 1)
        self.assertEqual(specs[1].start_byte, 1048576)
        self.assertEqual(specs[1].end_byte, 2097151)
        self.assertEqual(specs[1].size, 1048576)

    def test_canonical_json_serialization(self) -> None:
        """Test RFC 8785 canonical JSON excludes signature and sorts keys."""
        manifest = parse_manifest_json(json.dumps(_make_sample_manifest_dict()))
        canonical_bytes = manifest.to_canonical_json()

        parsed = json.loads(canonical_bytes.decode("utf-8"))
        self.assertNotIn("signature", parsed)
        self.assertIn("manifest_version", parsed)
        self.assertIn("chunks", parsed)

        # Canonical format must not have extraneous whitespace
        self.assertNotIn(b": ", canonical_bytes)
        self.assertNotIn(b", ", canonical_bytes)

    def test_invalid_json_syntax_raises_manifest_format_error(self) -> None:
        """Test that syntax errors in JSON raise ManifestFormatError."""
        with self.assertRaises(ManifestFormatError) as ctx:
            parse_manifest_json("{ bad: json, ")
        self.assertTrue(len(ctx.exception.schema_errors) > 0)

    def test_non_dict_root_raises_manifest_format_error(self) -> None:
        """Test that non-object JSON root raises ManifestFormatError."""
        with self.assertRaises(ManifestFormatError) as ctx:
            parse_manifest_json("[1, 2, 3]")
        self.assertIn("dictionary", ctx.exception.schema_errors[0].lower())

    def test_missing_required_fields_raises_manifest_format_error(self) -> None:
        """Test that omission of required root fields triggers schema error."""
        for required_field in ["manifest_version", "artifact_metadata", "chunking_topology", "chunks"]:
            sample = _make_sample_manifest_dict()
            del sample[required_field]
            with self.assertRaises(ManifestFormatError) as ctx:
                parse_manifest_json(json.dumps(sample))
            self.assertTrue(any(required_field in err for err in ctx.exception.schema_errors))

    def test_invalid_schema_field_values_raise_manifest_format_error(self) -> None:
        """Test schema validation rejection of invalid constraints."""
        # 1. Invalid manifest_version (must be "1.0.0")
        sample = _make_sample_manifest_dict()
        sample["manifest_version"] = "2.0.0"
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

        # 2. Negative file_size_bytes
        sample = _make_sample_manifest_dict()
        sample["artifact_metadata"]["file_size_bytes"] = -1
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

        # 3. Malformed SHA-256 hash
        sample = _make_sample_manifest_dict()
        sample["artifact_metadata"]["file_hash_sha256"] = "invalid_short_hash"
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

        # 4. default_chunk_size_bytes under minimum 1MB (1048576)
        sample = _make_sample_manifest_dict()
        sample["chunking_topology"]["default_chunk_size_bytes"] = 512
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

        # 5. Invalid mirror priority (> 100)
        sample = _make_sample_manifest_dict()
        sample["mirrors"][0]["priority"] = 999
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

        # 6. Invalid signature algorithm
        sample = _make_sample_manifest_dict()
        sample["signature"]["algorithm"] = "unsupported-sha1"
        with self.assertRaises(ManifestFormatError):
            parse_manifest_json(json.dumps(sample))

    def test_validate_manifest_dict_helper(self) -> None:
        """Test validate_manifest_dict returns error list directly."""
        sample = _make_sample_manifest_dict()
        errors = validate_manifest_dict(sample)
        self.assertEqual(errors, [])

        sample["manifest_version"] = "wrong"
        errors = validate_manifest_dict(sample)
        self.assertTrue(len(errors) > 0)

    def test_load_and_dump_manifest_disk(self) -> None:
        """Test saving to and reading from disk."""
        target_file = self.base_path / "test.cgmanifest"
        manifest = parse_manifest_json(json.dumps(_make_sample_manifest_dict()))

        # Save to disk
        dump_manifest(manifest, target_file)
        self.assertTrue(target_file.exists())

        # Load back
        loaded = load_manifest(target_file)
        self.assertEqual(loaded.artifact_metadata.filename, manifest.artifact_metadata.filename)
        self.assertEqual(len(loaded.chunks), len(manifest.chunks))

    def test_load_nonexistent_manifest_raises_manifest_error(self) -> None:
        """Test loading non-existent manifest file raises ManifestError."""
        target_file = self.base_path / "nonexistent.cgmanifest"
        with self.assertRaises(ManifestError):
            load_manifest(target_file)


if __name__ == "__main__":
    unittest.main()
