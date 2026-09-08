"""
ChunkGuard Manifest (.cgmanifest) JSON Schema parser, validator, and model bindings.
Implements Draft 2020-12 schema validation, Pydantic v2 domain models,
canonical RFC 8785 serialization, and ChunkSpec conversions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

import jsonschema
from jsonschema.validators import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.exceptions import ManifestError, ManifestFormatError
from src.models import ChunkSpec

# ─────────────────────────────────────────────────────────────────────────────
# JSON Schema Definition (Draft 2020-12)
# ─────────────────────────────────────────────────────────────────────────────

MANIFEST_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://chunkguard.org/schemas/v1/manifest.json",
    "title": "ChunkGuard Manifest",
    "type": "object",
    "required": [
        "manifest_version",
        "artifact_metadata",
        "chunking_topology",
        "chunks",
    ],
    "properties": {
        "manifest_version": {
            "type": "string",
            "const": "1.0.0",
            "description": "Semantic version of the manifest format",
        },
        "generator": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "timestamp": {"type": "string"},
            },
        },
        "artifact_metadata": {
            "type": "object",
            "required": ["filename", "file_size_bytes", "file_hash_sha256"],
            "properties": {
                "filename": {"type": "string"},
                "file_size_bytes": {"type": "integer", "minimum": 0},
                "file_hash_sha256": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{64}$",
                },
                "file_hash_sha512": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{128}$",
                },
                "content_type": {"type": "string"},
                "custom_metadata": {"type": "object"},
            },
        },
        "mirrors": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["url", "priority"],
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "region": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 100},
                    "headers": {"type": "object"},
                },
            },
        },
        "chunking_topology": {
            "type": "object",
            "required": ["default_chunk_size_bytes", "total_chunks", "hash_algorithm"],
            "properties": {
                "default_chunk_size_bytes": {"type": "integer", "minimum": 1048576},
                "total_chunks": {"type": "integer", "minimum": 1},
                "hash_algorithm": {"type": "string", "enum": ["sha256"]},
                "merkle_tree_root": {
                    "type": "string",
                    "pattern": "^[a-f0-9]{64}$",
                    "description": "Root hash of binary Merkle Tree over chunk hashes",
                },
            },
        },
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "start_byte", "end_byte", "size_bytes", "sha256"],
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "start_byte": {"type": "integer", "minimum": 0},
                    "end_byte": {"type": "integer", "minimum": 0},
                    "size_bytes": {"type": "integer", "minimum": 1},
                    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    "sub_blocks": {
                        "type": "array",
                        "description": "Optional 64KB sub-block hashes for streaming verification",
                        "items": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    },
                },
            },
        },
        "signature": {
            "type": "object",
            "required": ["algorithm", "key_id", "signature_hex"],
            "properties": {
                "algorithm": {"type": "string", "enum": ["ed25519", "rsa-pss-sha256"]},
                "key_id": {"type": "string"},
                "public_key": {"type": "string"},
                "signature_hex": {"type": "string"},
            },
        },
    },
}

_VALIDATOR = Draft202012Validator(MANIFEST_JSON_SCHEMA)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Domain Models
# ─────────────────────────────────────────────────────────────────────────────


class ManifestGenerator(BaseModel):
    """Information about tool that generated the manifest."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    version: Optional[str] = None
    timestamp: Optional[str] = None


class ArtifactMetadata(BaseModel):
    """Metadata describing the target download file."""

    model_config = ConfigDict(extra="ignore")

    filename: str
    file_size_bytes: int = Field(ge=0)
    file_hash_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_hash_sha512: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{128}$")
    content_type: Optional[str] = None
    custom_metadata: Optional[dict[str, Any]] = None

    @field_validator("file_hash_sha256", "file_hash_sha512", mode="before")
    @classmethod
    def normalize_hashes(cls, v: Optional[str]) -> Optional[str]:
        """Normalize hex hashes to lowercase without whitespace."""
        if v is not None and isinstance(v, str):
            return v.strip().lower()
        return v


class MirrorSpec(BaseModel):
    """Specification of an alternate download mirror."""

    model_config = ConfigDict(extra="ignore")

    url: str
    region: Optional[str] = None
    priority: int = Field(default=1, ge=1, le=100)
    headers: Optional[dict[str, str]] = None


class ChunkingTopology(BaseModel):
    """Topology parameters defining artifact chunking strategy."""

    model_config = ConfigDict(extra="ignore")

    default_chunk_size_bytes: int = Field(ge=1048576)
    total_chunks: int = Field(ge=1)
    hash_algorithm: str = "sha256"
    merkle_tree_root: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("merkle_tree_root", mode="before")
    @classmethod
    def normalize_root(cls, v: Optional[str]) -> Optional[str]:
        """Normalize hex root to lowercase without whitespace."""
        if v is not None and isinstance(v, str):
            return v.strip().lower()
        return v


class ManifestChunk(BaseModel):
    """Specification and pre-computed hash for a single chunk."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sub_blocks: Optional[list[str]] = None

    @field_validator("sha256", mode="before")
    @classmethod
    def normalize_chunk_hash(cls, v: str) -> str:
        """Normalize chunk hex hash."""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    def to_chunk_spec(self) -> ChunkSpec:
        """Convert manifest chunk definition to domain ChunkSpec."""
        return ChunkSpec(
            index=self.index,
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            size=self.size_bytes,
        )


class ManifestSignature(BaseModel):
    """Cryptographic signature guaranteeing manifest publisher authenticity."""

    model_config = ConfigDict(extra="ignore")

    algorithm: str = Field(pattern=r"^(ed25519|rsa-pss-sha256)$")
    key_id: str
    public_key: Optional[str] = None
    signature_hex: str


class ChunkManifest(BaseModel):
    """Complete root model for ChunkGuard .cgmanifest catalogs."""

    model_config = ConfigDict(extra="ignore")

    manifest_version: str = "1.0.0"
    generator: Optional[ManifestGenerator] = None
    artifact_metadata: ArtifactMetadata
    mirrors: list[MirrorSpec] = Field(default_factory=list)
    chunking_topology: ChunkingTopology
    chunks: list[ManifestChunk]
    signature: Optional[ManifestSignature] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize manifest model to Python dictionary matching JSON Schema."""
        return self.model_dump(mode="json", exclude_none=True)

    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize manifest model to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def to_canonical_json(self) -> bytes:
        """
        Serialize manifest per RFC 8785 JSON Canonicalization Scheme (JCS)
        with signature excluded, suitable for Ed25519 / RSA-PSS signing or verification.
        """
        payload = self.to_dict()
        payload.pop("signature", None)
        canonical_str = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return canonical_str.encode("utf-8")

    def get_chunk_specs(self) -> list[ChunkSpec]:
        """Convert all chunk definitions to a list of ChunkSpec domain objects."""
        return [c.to_chunk_spec() for c in self.chunks]

    def save(self, target_path: Union[str, Path]) -> None:
        """Save manifest to filesystem at target_path."""
        dump_manifest(self, target_path)


# ─────────────────────────────────────────────────────────────────────────────
# Schema Validation and Parsing Functions
# ─────────────────────────────────────────────────────────────────────────────


def validate_manifest_dict(
    data: dict[str, Any],
    manifest_path: Optional[str] = None,
) -> list[str]:
    """
    Validate a dictionary against the Draft 2020-12 manifest JSON Schema.

    Args:
        data: Manifest data dictionary.
        manifest_path: Optional path context for error reporting.

    Returns:
        List of formatted error message strings (empty if valid).
    """
    errors: list[str] = []
    for err in _VALIDATOR.iter_errors(data):
        path_str = " -> ".join(str(p) for p in err.absolute_path) or "root"
        errors.append(f"[{path_str}] {err.message}")
    return errors


def parse_manifest_json(
    content: str,
    manifest_path: Optional[str] = None,
) -> ChunkManifest:
    """
    Parse and validate a JSON string into a ChunkManifest instance.

    Args:
        content: Raw JSON string content.
        manifest_path: Optional path context for error messages.

    Returns:
        Validated ChunkManifest model instance.

    Raises:
        ManifestFormatError: If JSON syntax is invalid or violates schema.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as err:
        raise ManifestFormatError(
            f"Invalid JSON in manifest: {err}",
            manifest_path=manifest_path,
            schema_errors=[str(err)],
        )

    if not isinstance(data, dict):
        raise ManifestFormatError(
            f"Manifest root must be a JSON object, got {type(data).__name__}",
            manifest_path=manifest_path,
            schema_errors=["Root document is not a dictionary"],
        )

    schema_errors = validate_manifest_dict(data, manifest_path=manifest_path)
    if schema_errors:
        raise ManifestFormatError(
            f"Manifest schema validation failed with {len(schema_errors)} error(s)",
            manifest_path=manifest_path,
            schema_errors=schema_errors,
        )

    try:
        return ChunkManifest.model_validate(data)
    except Exception as err:
        raise ManifestFormatError(
            f"Failed to bind manifest model: {err}",
            manifest_path=manifest_path,
            schema_errors=[str(err)],
        )


def load_manifest(file_path: Union[str, Path]) -> ChunkManifest:
    """
    Load, parse, and validate a .cgmanifest file from disk.

    Args:
        file_path: Path to manifest file on disk.

    Returns:
        Validated ChunkManifest instance.

    Raises:
        ManifestError: If reading the file fails.
        ManifestFormatError: If parsing or validation fails.
    """
    path_obj = Path(file_path).resolve()
    try:
        content = path_obj.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ManifestError(
            f"Failed to read manifest file {path_obj}: {err}",
            manifest_path=str(path_obj),
        )

    return parse_manifest_json(content, manifest_path=str(path_obj))


def dump_manifest(
    manifest: ChunkManifest,
    file_path: Union[str, Path],
    indent: int = 2,
) -> None:
    """
    Serialize and save a ChunkManifest instance to disk.

    Args:
        manifest: Manifest instance to save.
        file_path: Target output path.
        indent: JSON indentation formatting.

    Raises:
        ManifestError: If writing to disk fails.
    """
    path_obj = Path(file_path).resolve()
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(manifest.to_json(indent=indent), encoding="utf-8")
    except OSError as err:
        raise ManifestError(
            f"Failed to write manifest file {path_obj}: {err}",
            manifest_path=str(path_obj),
        )
