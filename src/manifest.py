"""
ChunkGuard Manifest (.cgmanifest) JSON Schema parser, validator, model bindings,
and cryptographic signing/verification engine.
Implements Draft 2020-12 schema validation, Pydantic v2 domain models,
canonical RFC 8785 (JCS) serialization, Ed25519/RSA-PSS digital signatures,
and ChunkSpec conversions.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional, Union

import jsonschema
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from jsonschema.validators import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.exceptions import (
    ManifestError,
    ManifestFormatError,
    ManifestSignatureMismatchError,
)
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
# Canonical JSON (RFC 8785 JCS)
# ─────────────────────────────────────────────────────────────────────────────


def canonicalize_json(data: dict[str, Any]) -> bytes:
    """
    Serialize data dictionary to canonical JSON per RFC 8785 (JCS).
    Sorts keys lexicographically by Unicode code points and eliminates whitespace.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


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

    @property
    def is_signed(self) -> bool:
        """Whether the manifest contains a digital signature."""
        return self.signature is not None

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
        return canonicalize_json(payload)

    def get_chunk_specs(self) -> list[ChunkSpec]:
        """Convert all chunk definitions to a list of ChunkSpec domain objects."""
        return [c.to_chunk_spec() for c in self.chunks]

    def save(self, target_path: Union[str, Path]) -> None:
        """Save manifest to filesystem at target_path."""
        dump_manifest(self, target_path)

    def sign(
        self,
        private_key: Union[ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey, str, bytes],
        key_id: str,
        algorithm: str = "ed25519",
        include_public_key: bool = True,
    ) -> ChunkManifest:
        """Cryptographically sign this manifest in-place."""
        return sign_manifest(
            manifest=self,
            private_key=private_key,
            key_id=key_id,
            algorithm=algorithm,
            include_public_key=include_public_key,
        )

    def verify_signature(
        self,
        trusted_public_key: Optional[Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey, str, bytes]] = None,
        manifest_path: Optional[str] = None,
    ) -> bool:
        """Cryptographically verify this manifest's digital signature."""
        return verify_manifest_signature(
            manifest=self,
            trusted_public_key=trusted_public_key,
            manifest_path=manifest_path,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Key Management and Cryptographic Operations
# ─────────────────────────────────────────────────────────────────────────────


def generate_ed25519_keypair() -> tuple[ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey]:
    """Generate a new Ed25519 private/public keypair."""
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return priv, pub


def generate_rsa_keypair(key_size: int = 2048) -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """Generate a new RSA private/public keypair (for RSA-PSS signing)."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    pub = priv.public_key()
    return priv, pub


def public_key_to_base64(public_key: Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey]) -> str:
    """Encode public key to base64 string."""
    if isinstance(public_key, ed25519.Ed25519PublicKey):
        raw_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw_bytes).decode("ascii")
    elif isinstance(public_key, rsa.RSAPublicKey):
        der_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der_bytes).decode("ascii")
    raise ValueError(f"Unsupported public key type: {type(public_key)}")


def public_key_to_pem(public_key: Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey]) -> str:
    """Export public key in PEM format string."""
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem_bytes.decode("utf-8")


def private_key_to_pem(private_key: Union[ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey]) -> str:
    """Export private key in PKCS8 unencrypted PEM format string."""
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem_bytes.decode("utf-8")


def load_public_key(
    key_input: Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey, str, bytes],
    algorithm: str = "ed25519",
) -> Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey]:
    """
    Parse a public key from an object, PEM string, base64 string, or raw bytes.

    Args:
        key_input: Public key in any supported format.
        algorithm: Expected algorithm ('ed25519' or 'rsa-pss-sha256').

    Returns:
        Ed25519PublicKey or RSAPublicKey instance.
    """
    if isinstance(key_input, (ed25519.Ed25519PublicKey, rsa.RSAPublicKey)):
        return key_input

    raw_bytes = key_input.encode("utf-8") if isinstance(key_input, str) else key_input

    # PEM format
    if b"-----BEGIN" in raw_bytes:
        loaded = serialization.load_pem_public_key(raw_bytes)
        if isinstance(loaded, (ed25519.Ed25519PublicKey, rsa.RSAPublicKey)):
            return loaded
        raise ValueError(f"Loaded key {type(loaded)} is not an Ed25519 or RSA public key")

    # Raw or base64
    try:
        decoded = base64.b64decode(raw_bytes, validate=True)
    except Exception:
        decoded = raw_bytes

    if algorithm == "ed25519":
        if len(decoded) == 32:
            return ed25519.Ed25519PublicKey.from_public_bytes(decoded)
        # Try DER
        try:
            return serialization.load_der_public_key(decoded)  # type: ignore[return-value]
        except Exception as err:
            raise ValueError(f"Invalid Ed25519 public key bytes ({len(decoded)} bytes): {err}")
    else:
        # RSA
        try:
            return serialization.load_der_public_key(decoded)  # type: ignore[return-value]
        except Exception as err:
            raise ValueError(f"Invalid RSA public key bytes: {err}")


def load_private_key(
    key_input: Union[ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey, str, bytes],
    algorithm: str = "ed25519",
) -> Union[ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey]:
    """
    Parse a private key from an object, PEM string, base64 string, or raw bytes.

    Args:
        key_input: Private key in any supported format.
        algorithm: Expected algorithm ('ed25519' or 'rsa-pss-sha256').

    Returns:
        Ed25519PrivateKey or RSAPrivateKey instance.
    """
    if isinstance(key_input, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
        return key_input

    raw_bytes = key_input.encode("utf-8") if isinstance(key_input, str) else key_input

    # PEM format
    if b"-----BEGIN" in raw_bytes:
        loaded = serialization.load_pem_private_key(raw_bytes, password=None)
        if isinstance(loaded, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            return loaded
        raise ValueError(f"Loaded key {type(loaded)} is not an Ed25519 or RSA private key")

    # Raw or base64
    try:
        decoded = base64.b64decode(raw_bytes, validate=True)
    except Exception:
        decoded = raw_bytes

    if algorithm == "ed25519":
        if len(decoded) == 32:
            return ed25519.Ed25519PrivateKey.from_private_bytes(decoded)
        try:
            return serialization.load_der_private_key(decoded, password=None)  # type: ignore[return-value]
        except Exception as err:
            raise ValueError(f"Invalid Ed25519 private key bytes ({len(decoded)} bytes): {err}")
    else:
        try:
            return serialization.load_der_private_key(decoded, password=None)  # type: ignore[return-value]
        except Exception as err:
            raise ValueError(f"Invalid RSA private key bytes: {err}")


def sign_manifest(
    manifest: ChunkManifest,
    private_key: Union[ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey, str, bytes],
    key_id: str,
    algorithm: str = "ed25519",
    include_public_key: bool = True,
) -> ChunkManifest:
    """
    Digitally sign a manifest per RFC 8785 JCS canonicalization.

    Args:
        manifest: Manifest to sign.
        private_key: Ed25519 or RSA private key.
        key_id: Key identifier (e.g. 'release-2026-q3').
        algorithm: 'ed25519' or 'rsa-pss-sha256'.
        include_public_key: Embed base64-encoded public key in manifest.

    Returns:
        The updated manifest instance with signature attached.
    """
    normalized_algo = algorithm.lower().strip()
    if normalized_algo not in ("ed25519", "rsa-pss-sha256"):
        raise ValueError(f"Unsupported signing algorithm: {algorithm}")

    priv_key_obj = load_private_key(private_key, algorithm=normalized_algo)
    canonical_payload = manifest.to_canonical_json()

    if normalized_algo == "ed25519":
        if not isinstance(priv_key_obj, ed25519.Ed25519PrivateKey):
            raise ValueError("Algorithm is ed25519 but provided key is not Ed25519PrivateKey")
        sig_bytes = priv_key_obj.sign(canonical_payload)
        pub_key_b64 = public_key_to_base64(priv_key_obj.public_key()) if include_public_key else None
    else:
        if not isinstance(priv_key_obj, rsa.RSAPrivateKey):
            raise ValueError("Algorithm is rsa-pss-sha256 but provided key is not RSAPrivateKey")
        sig_bytes = priv_key_obj.sign(
            canonical_payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        pub_key_b64 = public_key_to_base64(priv_key_obj.public_key()) if include_public_key else None

    manifest.signature = ManifestSignature(
        algorithm=normalized_algo,
        key_id=key_id,
        public_key=pub_key_b64,
        signature_hex=sig_bytes.hex(),
    )
    return manifest


def verify_manifest_signature(
    manifest: ChunkManifest,
    trusted_public_key: Optional[Union[ed25519.Ed25519PublicKey, rsa.RSAPublicKey, str, bytes]] = None,
    manifest_path: Optional[str] = None,
) -> bool:
    """
    Verify the cryptographic digital signature of a ChunkManifest.

    Args:
        manifest: Manifest instance to verify.
        trusted_public_key: Explicit trusted public key. If None, uses embedded public key.
        manifest_path: Optional path for error context.

    Returns:
        True if signature is valid.

    Raises:
        ManifestSignatureMismatchError: If signature verification fails or key is missing.
    """
    if manifest.signature is None:
        raise ManifestSignatureMismatchError(
            "Manifest has no digital signature block",
            manifest_path=manifest_path,
        )

    sig_spec = manifest.signature
    key_id = sig_spec.key_id
    algo = sig_spec.algorithm

    # Determine public key source
    key_to_use = trusted_public_key
    if key_to_use is None:
        if sig_spec.public_key:
            key_to_use = sig_spec.public_key
        else:
            raise ManifestSignatureMismatchError(
                f"No public key supplied and manifest signature (key_id='{key_id}') contains no embedded key",
                key_id=key_id,
                algorithm=algo,
                manifest_path=manifest_path,
            )

    try:
        pub_key_obj = load_public_key(key_to_use, algorithm=algo)
    except Exception as err:
        raise ManifestSignatureMismatchError(
            f"Failed to parse public key for verification: {err}",
            key_id=key_id,
            algorithm=algo,
            manifest_path=manifest_path,
        )

    try:
        sig_bytes = bytes.fromhex(sig_spec.signature_hex)
    except ValueError as err:
        raise ManifestSignatureMismatchError(
            f"Malformed signature hex string in manifest: {err}",
            key_id=key_id,
            algorithm=algo,
            manifest_path=manifest_path,
        )

    canonical_payload = manifest.to_canonical_json()

    if algo == "ed25519":
        if not isinstance(pub_key_obj, ed25519.Ed25519PublicKey):
            raise ManifestSignatureMismatchError(
                "Signature algorithm is ed25519 but public key is not Ed25519PublicKey",
                key_id=key_id,
                algorithm=algo,
                manifest_path=manifest_path,
            )
        try:
            pub_key_obj.verify(sig_bytes, canonical_payload)
            return True
        except InvalidSignature:
            raise ManifestSignatureMismatchError(
                f"Ed25519 digital signature validation failed for key_id='{key_id}'",
                key_id=key_id,
                algorithm=algo,
                manifest_path=manifest_path,
            )

    elif algo == "rsa-pss-sha256":
        if not isinstance(pub_key_obj, rsa.RSAPublicKey):
            raise ManifestSignatureMismatchError(
                "Signature algorithm is rsa-pss-sha256 but public key is not RSAPublicKey",
                key_id=key_id,
                algorithm=algo,
                manifest_path=manifest_path,
            )
        try:
            pub_key_obj.verify(
                sig_bytes,
                canonical_payload,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ),
                hashes.SHA256(),
            )
            return True
        except InvalidSignature:
            raise ManifestSignatureMismatchError(
                f"RSA-PSS digital signature validation failed for key_id='{key_id}'",
                key_id=key_id,
                algorithm=algo,
                manifest_path=manifest_path,
            )

    raise ManifestSignatureMismatchError(
        f"Unsupported signature algorithm '{algo}'",
        key_id=key_id,
        algorithm=algo,
        manifest_path=manifest_path,
    )


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
