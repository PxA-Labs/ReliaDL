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

from src.adapters.gcs_adapter import (
    CREDENTIALS_ENV,
    GCS_HOST,
    GCS_READ_SCOPE,
    GCS_TOKEN_URI,
    GCSAdapter,
    GCSServiceAccount,
    find_application_default_credentials,
)
from src.adapters.azure_adapter import (
    AZURE_API_VERSION,
    AZURE_BLOB_SUFFIX,
    AzureAuthMode,
    AzureBlobAdapter,
    AzureSharedKeyCredential,
    AzureSharedKeySigner,
    format_rfc1123,
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
    # Google Cloud Storage
    "GCS_HOST",
    "GCS_TOKEN_URI",
    "GCS_READ_SCOPE",
    "CREDENTIALS_ENV",
    "GCSServiceAccount",
    "GCSAdapter",
    "find_application_default_credentials",
    # Azure Blob Storage
    "AZURE_BLOB_SUFFIX",
    "AZURE_API_VERSION",
    "AzureAuthMode",
    "AzureSharedKeyCredential",
    "AzureSharedKeySigner",
    "AzureBlobAdapter",
    "format_rfc1123",
]
