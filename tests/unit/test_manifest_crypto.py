"""
Unit tests for cryptographic manifest signing and verification (Ed25519 and RSA-PSS).
"""

from __future__ import annotations

import json
import unittest

from src.exceptions import ManifestSignatureMismatchError
from src.manifest import (
    ArtifactMetadata,
    ChunkingTopology,
    ChunkManifest,
    ManifestChunk,
    ManifestGenerator,
    canonicalize_json,
    generate_ed25519_keypair,
    generate_rsa_keypair,
    load_private_key,
    load_public_key,
    private_key_to_pem,
    public_key_to_base64,
    public_key_to_pem,
    sign_manifest,
    verify_manifest_signature,
)


def _make_base_manifest() -> ChunkManifest:
    """Helper to create a fresh unsigned test manifest."""
    return ChunkManifest(
        manifest_version="1.0.0",
        generator=ManifestGenerator(name="Crypto Test", version="1.0.0"),
        artifact_metadata=ArtifactMetadata(
            filename="release-v1.0.tar.gz",
            file_size_bytes=2097152,
            file_hash_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ),
        chunking_topology=ChunkingTopology(
            default_chunk_size_bytes=1048576,
            total_chunks=2,
            hash_algorithm="sha256",
            merkle_tree_root="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        ),
        chunks=[
            ManifestChunk(
                index=0,
                start_byte=0,
                end_byte=1048575,
                size_bytes=1048576,
                sha256="1111111111111111111111111111111111111111111111111111111111111111",
            ),
            ManifestChunk(
                index=1,
                start_byte=1048576,
                end_byte=2097151,
                size_bytes=1048576,
                sha256="2222222222222222222222222222222222222222222222222222222222222222",
            ),
        ],
    )


class TestManifestCrypto(unittest.TestCase):
    """Test suite for Ed25519 and RSA-PSS digital signing and verification."""

    def test_ed25519_sign_and_verify_success(self) -> None:
        """Test happy-path Ed25519 signing and signature verification."""
        priv, pub = generate_ed25519_keypair()
        manifest = _make_base_manifest()

        self.assertFalse(manifest.is_signed)
        signed = sign_manifest(manifest, priv, key_id="rel-2026-ed", algorithm="ed25519")

        self.assertTrue(signed.is_signed)
        self.assertEqual(signed.signature.algorithm, "ed25519")
        self.assertEqual(signed.signature.key_id, "rel-2026-ed")
        self.assertIsNotNone(signed.signature.public_key)

        # Verification via embedded public key
        self.assertTrue(verify_manifest_signature(signed))

        # Verification via explicit trusted public key
        self.assertTrue(verify_manifest_signature(signed, trusted_public_key=pub))

        # Verification via convenience method on model
        self.assertTrue(signed.verify_signature())

    def test_ed25519_tampered_payload_rejected(self) -> None:
        """
        Acceptance criterion: Tampered manifest payload rejected with ManifestSignatureMismatchError.
        """
        priv, pub = generate_ed25519_keypair()
        manifest = _make_base_manifest()
        manifest.sign(priv, key_id="rel-2026-ed", algorithm="ed25519")

        # 1. Tamper with artifact metadata filename
        manifest.artifact_metadata.filename = "malicious_payload.iso"
        with self.assertRaises(ManifestSignatureMismatchError) as ctx:
            manifest.verify_signature(trusted_public_key=pub)
        self.assertEqual(ctx.exception.key_id, "rel-2026-ed")
        self.assertEqual(ctx.exception.algorithm, "ed25519")

        # 2. Tamper with chunk hash
        manifest.artifact_metadata.filename = "release-v1.0.tar.gz"  # restore
        manifest.chunks[0].sha256 = "9" * 64
        with self.assertRaises(ManifestSignatureMismatchError):
            manifest.verify_signature(trusted_public_key=pub)

    def test_rsa_pss_sign_and_verify_success(self) -> None:
        """Test RSA-PSS SHA-256 signing and verification."""
        priv, pub = generate_rsa_keypair(key_size=2048)
        manifest = _make_base_manifest()

        manifest.sign(priv, key_id="rel-2026-rsa", algorithm="rsa-pss-sha256")
        self.assertTrue(manifest.is_signed)
        self.assertEqual(manifest.signature.algorithm, "rsa-pss-sha256")

        # Verify
        self.assertTrue(manifest.verify_signature())
        self.assertTrue(verify_manifest_signature(manifest, trusted_public_key=pub))

    def test_rsa_pss_tampered_payload_rejected(self) -> None:
        """Test that tampered payload under RSA-PSS raises ManifestSignatureMismatchError."""
        priv, pub = generate_rsa_keypair(key_size=2048)
        manifest = _make_base_manifest()
        manifest.sign(priv, key_id="rel-2026-rsa", algorithm="rsa-pss-sha256")

        manifest.chunking_topology.total_chunks = 99
        with self.assertRaises(ManifestSignatureMismatchError) as ctx:
            manifest.verify_signature(trusted_public_key=pub)
        self.assertEqual(ctx.exception.key_id, "rel-2026-rsa")
        self.assertEqual(ctx.exception.algorithm, "rsa-pss-sha256")

    def test_wrong_public_key_rejected(self) -> None:
        """Test that verification with an unrelated public key fails."""
        priv_a, _ = generate_ed25519_keypair()
        _, pub_b = generate_ed25519_keypair()

        manifest = _make_base_manifest()
        manifest.sign(priv_a, key_id="key-a")

        with self.assertRaises(ManifestSignatureMismatchError):
            verify_manifest_signature(manifest, trusted_public_key=pub_b)

    def test_tampered_signature_hex_rejected(self) -> None:
        """Test that altered signature bytes or invalid hex raise ManifestSignatureMismatchError."""
        priv, pub = generate_ed25519_keypair()
        manifest = _make_base_manifest()
        manifest.sign(priv, key_id="key-ed")

        # Corrupt signature hex
        sig_hex = manifest.signature.signature_hex
        tampered_hex = ("0" if sig_hex[0] != "0" else "1") + sig_hex[1:]
        manifest.signature.signature_hex = tampered_hex

        with self.assertRaises(ManifestSignatureMismatchError):
            manifest.verify_signature()

        # Non-hex string
        manifest.signature.signature_hex = "not_a_valid_hex_string"
        with self.assertRaises(ManifestSignatureMismatchError):
            manifest.verify_signature()

    def test_unsigned_manifest_verification_raises(self) -> None:
        """Test verifying unsigned manifest raises ManifestSignatureMismatchError."""
        manifest = _make_base_manifest()
        with self.assertRaises(ManifestSignatureMismatchError):
            manifest.verify_signature()

    def test_missing_embedded_and_trusted_public_key_raises(self) -> None:
        """Test verifying manifest without embedded or external public key raises error."""
        priv, pub = generate_ed25519_keypair()
        manifest = _make_base_manifest()
        sign_manifest(manifest, priv, key_id="key-no-pub", include_public_key=False)

        with self.assertRaises(ManifestSignatureMismatchError):
            verify_manifest_signature(manifest, trusted_public_key=None)

        # But succeeds when trusted key is passed
        self.assertTrue(verify_manifest_signature(manifest, trusted_public_key=pub))

    def test_key_serialization_roundtrips(self) -> None:
        """Test key format encodings (PEM, Base64, raw)."""
        priv_ed, pub_ed = generate_ed25519_keypair()

        # Base64 roundtrip
        b64 = public_key_to_base64(pub_ed)
        pub_loaded = load_public_key(b64, algorithm="ed25519")
        self.assertEqual(public_key_to_base64(pub_loaded), b64)

        # PEM roundtrip
        pub_pem = public_key_to_pem(pub_ed)
        self.assertIn("-----BEGIN PUBLIC KEY-----", pub_pem)
        pub_pem_loaded = load_public_key(pub_pem, algorithm="ed25519")
        self.assertEqual(public_key_to_base64(pub_pem_loaded), b64)

        # Private key PEM
        priv_pem = private_key_to_pem(priv_ed)
        self.assertIn("-----BEGIN PRIVATE KEY-----", priv_pem)
        priv_loaded = load_private_key(priv_pem, algorithm="ed25519")
        self.assertEqual(
            public_key_to_base64(priv_loaded.public_key()),
            b64,
        )

    def test_canonicalize_json_order_invariance(self) -> None:
        """Test that canonicalize_json produces identical bytes regardless of key insertion order."""
        dict1 = {"b": 2, "a": 1, "c": {"y": "test", "x": [1, 2, 3]}}
        dict2 = {"a": 1, "c": {"x": [1, 2, 3], "y": "test"}, "b": 2}

        c1 = canonicalize_json(dict1)
        c2 = canonicalize_json(dict2)

        self.assertEqual(c1, c2)
        self.assertEqual(c1, b'{"a":1,"b":2,"c":{"x":[1,2,3],"y":"test"}}')


if __name__ == "__main__":
    unittest.main()
