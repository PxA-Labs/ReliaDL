"""
Unit tests for domain-separated binary Merkle tree engine and SBM-IA streaming sub-block validator.
"""

from __future__ import annotations

import hashlib
import unittest

from src.exceptions import (
    ChunkHashMismatchError,
    ManifestError,
    SubBlockCorruptedError,
)
from src.manifest import (
    ArtifactMetadata,
    BinaryMerkleTree,
    ChunkingTopology,
    ChunkManifest,
    ManifestChunk,
    SubBlockStreamValidator,
    calculate_sub_blocks,
    compute_merkle_root,
    hash_leaf,
    hash_parent,
)


class TestBinaryMerkleTree(unittest.TestCase):
    """Test suite for binary Merkle tree construction and inclusion proofs."""

    def test_known_test_tree_roots(self) -> None:
        """
        Acceptance criterion: Merkle root calculation matching known test trees.
        Verifies domain separation: 0x00 || leaf, 0x01 || left || right,
        and odd balancing: 0x01 || L || L.
        """
        # Test vectors
        h0 = "a" * 64
        h1 = "b" * 64
        h2 = "c" * 64
        h3 = "d" * 64

        leaf0 = hashlib.sha256(b"\x00" + bytes.fromhex(h0)).digest()
        leaf1 = hashlib.sha256(b"\x00" + bytes.fromhex(h1)).digest()
        leaf2 = hashlib.sha256(b"\x00" + bytes.fromhex(h2)).digest()
        leaf3 = hashlib.sha256(b"\x00" + bytes.fromhex(h3)).digest()

        # 1-leaf tree
        expected_root_1 = leaf0.hex()
        self.assertEqual(compute_merkle_root([h0]), expected_root_1)

        # 2-leaf tree
        expected_root_2 = hashlib.sha256(b"\x01" + leaf0 + leaf1).hexdigest()
        self.assertEqual(compute_merkle_root([h0, h1]), expected_root_2)

        # 3-leaf tree (odd node duplication at level 0)
        parent_0_1 = hashlib.sha256(b"\x01" + leaf0 + leaf1).digest()
        parent_2_2 = hashlib.sha256(b"\x01" + leaf2 + leaf2).digest()
        expected_root_3 = hashlib.sha256(b"\x01" + parent_0_1 + parent_2_2).hexdigest()
        self.assertEqual(compute_merkle_root([h0, h1, h2]), expected_root_3)

        # 4-leaf tree
        parent_2_3 = hashlib.sha256(b"\x01" + leaf2 + leaf3).digest()
        expected_root_4 = hashlib.sha256(b"\x01" + parent_0_1 + parent_2_3).hexdigest()
        self.assertEqual(compute_merkle_root([h0, h1, h2, h3]), expected_root_4)

    def test_empty_chunk_list_root(self) -> None:
        """Test that empty chunk list returns empty string root."""
        self.assertEqual(compute_merkle_root([]), "")

    def test_merkle_tree_class_properties(self) -> None:
        """Test BinaryMerkleTree levels, root, and leaf nodes."""
        hashes = [hashlib.sha256(f"chunk-{i}".encode()).hexdigest() for i in range(5)]
        tree = BinaryMerkleTree(hashes)

        self.assertEqual(len(tree.leaf_nodes), 5)
        # Levels: level 0 (5 leaves) -> level 1 (3 nodes) -> level 2 (2 nodes) -> level 3 (1 root)
        self.assertEqual(len(tree.levels), 4)
        self.assertEqual(tree.root, compute_merkle_root(hashes))

    def test_inclusion_proof_verification(self) -> None:
        """Test generating and verifying inclusion proofs for every leaf in an 8-leaf tree."""
        hashes = [hashlib.sha256(f"data-{i}".encode()).hexdigest() for i in range(8)]
        tree = BinaryMerkleTree(hashes)

        for i in range(8):
            proof = tree.get_proof(i)
            # Valid proof succeeds
            self.assertTrue(BinaryMerkleTree.verify_inclusion(hashes[i], proof, tree.root))

            # Tampered leaf hash fails
            wrong_hash = hashlib.sha256(b"tampered").hexdigest()
            self.assertFalse(BinaryMerkleTree.verify_inclusion(wrong_hash, proof, tree.root))

            # Wrong expected root fails
            self.assertFalse(BinaryMerkleTree.verify_inclusion(hashes[i], proof, "0" * 64))

    def test_manifest_merkle_root_integration(self) -> None:
        """Test compute_merkle_root and verify_merkle_root on ChunkManifest."""
        h0 = "1" * 64
        h1 = "2" * 64
        expected_root = compute_merkle_root([h0, h1])

        manifest = ChunkManifest(
            artifact_metadata=ArtifactMetadata(
                filename="archive.tar",
                file_size_bytes=2097152,
                file_hash_sha256="3" * 64,
            ),
            chunking_topology=ChunkingTopology(
                default_chunk_size_bytes=1048576,
                total_chunks=2,
                hash_algorithm="sha256",
                merkle_tree_root=expected_root,
            ),
            chunks=[
                ManifestChunk(index=0, start_byte=0, end_byte=1048575, size_bytes=1048576, sha256=h0),
                ManifestChunk(index=1, start_byte=1048576, end_byte=2097151, size_bytes=1048576, sha256=h1),
            ],
        )

        self.assertEqual(manifest.compute_merkle_root(), expected_root)
        self.assertTrue(manifest.verify_merkle_root())

        # Tamper with chunk
        manifest.chunks[0].sha256 = "4" * 64
        with self.assertRaises(ManifestError):
            manifest.verify_merkle_root()


class TestSubBlockStreamValidator(unittest.TestCase):
    """Test suite for SBM-IA streaming 64 KB sub-block verification."""

    def test_clean_stream_verification_success(self) -> None:
        """Test streaming full chunk with valid sub-blocks completes cleanly."""
        # 192 KB chunk = 3 x 64 KB sub-blocks
        sub_block_size = 64 * 1024
        chunk_data = b"A" * sub_block_size + b"B" * sub_block_size + b"C" * sub_block_size
        sub_blocks = calculate_sub_blocks(chunk_data, sub_block_size=sub_block_size)
        chunk_hash = hashlib.sha256(chunk_data).hexdigest()

        validator = SubBlockStreamValidator(
            chunk_index=0,
            expected_sub_blocks=sub_blocks,
            sub_block_size=sub_block_size,
            expected_chunk_hash=chunk_hash,
        )

        # Feed in small 4 KB packets
        packet_size = 4 * 1024
        for offset in range(0, len(chunk_data), packet_size):
            validator.update(chunk_data[offset : offset + packet_size])

        self.assertEqual(validator.verified_sub_blocks, 3)
        self.assertTrue(validator.finalize())
        self.assertTrue(validator.is_finalized)

    def test_corrupted_sub_block_immediate_stream_abort(self) -> None:
        """
        Acceptance criterion: Corrupted 64 KB sub-block triggers immediate stream abort.
        When corruption occurs in sub-block 1 (between 64KB and 128KB),
        the validator immediately raises SubBlockCorruptedError upon completing block 1,
        never requiring subsequent blocks to be received.
        """
        sub_block_size = 64 * 1024
        # 256 KB = 4 x 64 KB sub-blocks
        block0 = b"0" * sub_block_size
        block1 = b"1" * sub_block_size
        block2 = b"2" * sub_block_size
        block3 = b"3" * sub_block_size

        original_chunk = block0 + block1 + block2 + block3
        expected_sub_blocks = calculate_sub_blocks(original_chunk, sub_block_size=sub_block_size)

        # Corrupt block 1 (flip byte at offset 10 inside block 1)
        corrupted_block1 = b"1" * 10 + b"X" + b"1" * (sub_block_size - 11)

        validator = SubBlockStreamValidator(
            chunk_index=5,
            expected_sub_blocks=expected_sub_blocks,
            sub_block_size=sub_block_size,
        )

        # Feed block 0 -> verifies successfully
        validator.update(block0)
        self.assertEqual(validator.verified_sub_blocks, 1)

        # Feed corrupted block 1 in increments of 8 KB
        with self.assertRaises(SubBlockCorruptedError) as ctx:
            for i in range(0, len(corrupted_block1), 8 * 1024):
                validator.update(corrupted_block1[i : i + 8 * 1024])

        # Verify immediate abort diagnostics
        err = ctx.exception
        self.assertEqual(err.chunk_index, 5)
        self.assertEqual(err.sub_block_index, 1)
        self.assertEqual(err.expected_hash, expected_sub_blocks[1])
        self.assertNotEqual(err.computed_hash, expected_sub_blocks[1])

        # Blocks 2 and 3 were NEVER downloaded/processed (stream aborted immediately!)
        self.assertEqual(validator.total_bytes_processed, len(block0) + len(corrupted_block1))

    def test_trailing_partial_sub_block_verification(self) -> None:
        """Test verification of trailing partial sub-block (e.g. 100 KB = 64 KB + 36 KB)."""
        sub_block_size = 64 * 1024
        data = b"M" * (100 * 1024)
        sub_blocks = calculate_sub_blocks(data, sub_block_size=sub_block_size)
        self.assertEqual(len(sub_blocks), 2)  # 64KB + 36KB

        validator = SubBlockStreamValidator(
            chunk_index=1,
            expected_sub_blocks=sub_blocks,
            sub_block_size=sub_block_size,
            expected_chunk_hash=hashlib.sha256(data).hexdigest(),
        )

        validator.update(data)
        self.assertEqual(validator.verified_sub_blocks, 1)  # Only full 64KB verified on update

        # Finalize verifies the remaining 36KB
        self.assertTrue(validator.finalize())
        self.assertEqual(validator.verified_sub_blocks, 2)

    def test_corrupted_trailing_sub_block_raises_on_finalize(self) -> None:
        """Test that corruption in trailing partial sub-block is caught upon finalize."""
        sub_block_size = 64 * 1024
        data = b"M" * (100 * 1024)
        sub_blocks = calculate_sub_blocks(data, sub_block_size=sub_block_size)

        validator = SubBlockStreamValidator(
            chunk_index=2,
            expected_sub_blocks=sub_blocks,
            sub_block_size=sub_block_size,
        )

        # Feed clean 64KB, then corrupted 36KB
        validator.update(b"M" * (64 * 1024))
        validator.update(b"BAD_TRAILING_DATA" + b"M" * (36 * 1024 - 17))

        with self.assertRaises(SubBlockCorruptedError) as ctx:
            validator.finalize()
        self.assertEqual(ctx.exception.sub_block_index, 1)


if __name__ == "__main__":
    unittest.main()
