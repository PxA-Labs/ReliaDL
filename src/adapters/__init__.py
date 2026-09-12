"""
Cloud storage and enterprise protocol adapters for ReliaDL.

Each adapter resolves a provider-specific URI and signs a byte-range request for
it, leaving the transport to issue it.
"""

from __future__ import annotations

from src.adapters.base import (
    BaseRangeAdapter,
    ObjectLocation,
    SignedRequest,
    format_range_header,
)

from src.adapters.s3_adapter import (
    ALGORITHM,
    DEFAULT_REGION,
    EMPTY_PAYLOAD_SHA256,
    AWSCredentials,
    S3Adapter,
    SigV4Signer,
    credentials_from_environment,
    credentials_from_instance_metadata,
    credentials_from_shared_file,
    is_dns_compatible_bucket,
    resolve_credentials,
)

__all__ = [
    "format_range_header",
    "SignedRequest",
    "ObjectLocation",
    "BaseRangeAdapter",
    # AWS S3
    "ALGORITHM",
    "DEFAULT_REGION",
    "EMPTY_PAYLOAD_SHA256",
    "AWSCredentials",
    "SigV4Signer",
    "S3Adapter",
    "credentials_from_environment",
    "credentials_from_shared_file",
    "credentials_from_instance_metadata",
    "resolve_credentials",
    "is_dns_compatible_bucket",
]
