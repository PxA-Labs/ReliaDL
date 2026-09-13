"""
Shared contract for ReliaDL's cloud storage and protocol adapters.

An adapter's job is to turn a provider-specific URI and a byte range into a
request the transport can issue: the resolved endpoint, and whatever headers
that provider's authentication scheme demands. Every cloud object store speaks
HTTP range requests underneath; what differs is how the request is addressed
and signed, and that difference is all these adapters encode.

Relationship to the documented interface
---------------------------------------
``docs/CLOUD_ADAPTERS.md`` specifies an async ``BaseStorageAdapter`` whose
``fetch_range`` streams bytes. That interface needs an HTTP client, which the
project does not yet have — the httpx engine is a separate piece of work. The
contract here is the half that can be built and tested now and that the streaming
interface will need regardless: request *preparation*, kept free of I/O.

The split is worth keeping even once the transport exists. Signing is pure
computation over a request description, so it can be verified against published
test vectors byte for byte, which is the only way to be confident a signature is
right — an adapter that performs its own I/O can only be checked against a mock
that shares its author's misunderstandings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from src.exceptions import ConfigurationError


def format_range_header(start_byte: int, end_byte: int) -> str:
    """
    Render an inclusive byte range as an HTTP Range header value.

    Both endpoints are inclusive, matching RFC 9110 and ``ChunkSpec``. The
    inclusive convention is a frequent source of off-by-one errors precisely
    because a half-open range is the more common idiom in code.

    Raises:
        ConfigurationError: If either endpoint is not a non-negative integer,
            or the range runs backwards.
    """
    for name, value in (("start_byte", start_byte), ("end_byte", end_byte)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(
                f"{name} must be an integer, got {type(value).__name__}",
                parameter=name,
                value=value,
            )
        if value < 0:
            raise ConfigurationError(
                f"{name} must be non-negative, got {value}",
                parameter=name,
                value=value,
            )
    if end_byte < start_byte:
        raise ConfigurationError(
            f"end_byte ({end_byte}) cannot precede start_byte ({start_byte})",
            parameter="end_byte",
            value=end_byte,
        )
    return f"bytes={start_byte}-{end_byte}"


@dataclass(frozen=True)
class SignedRequest:
    """
    A range request ready for the transport to issue.

    Immutable because it is the signed artifact: with most cloud schemes the
    headers are covered by a signature, so mutating one after the fact yields a
    request the provider will reject with an authentication error rather than a
    helpful one. Anything the caller needs to change has to be changed before
    signing.

    Attributes:
        method: HTTP method, always GET for range reads.
        url: Absolute URL to request.
        headers: Complete header set, including authentication.
        start_byte: First byte requested, inclusive.
        end_byte: Last byte requested, inclusive.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ConfigurationError(
                "url must be a non-empty string", parameter="url", value=self.url
            )
        # Freeze the headers too: a mapping handed in by a caller who keeps a
        # reference could otherwise be edited after signing.
        object.__setattr__(self, "headers", dict(self.headers))

    @property
    def size(self) -> int:
        """Number of bytes the range covers."""
        return self.end_byte - self.start_byte + 1

    @property
    def range_header(self) -> str:
        """The range this request asks for, in HTTP form."""
        return format_range_header(self.start_byte, self.end_byte)

    def header(self, name: str) -> Optional[str]:
        """
        Look up a header case-insensitively.

        HTTP header names are case-insensitive, and providers are inconsistent
        about how they capitalize them, so an exact-match lookup would work by
        luck.
        """
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None

    def __repr__(self) -> str:
        return (
            f"SignedRequest({self.method} {self.url} "
            f"range={self.start_byte}-{self.end_byte}, "
            f"headers={len(self.headers)})"
        )


@dataclass(frozen=True)
class ObjectLocation:
    """
    A parsed object reference: which store, which container, which object.

    Attributes:
        scheme: URI scheme the reference was written in.
        container: Bucket, container, or share holding the object.
        key: Path of the object within its container.
        region: Region, when the URI or configuration names one.
        endpoint: Explicit host, for presigned or already-resolved URLs.
    """

    scheme: str
    container: str
    key: str
    region: Optional[str] = None
    endpoint: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.container:
            raise ConfigurationError(
                f"{self.scheme} URI is missing a container or bucket name",
                parameter="container",
                value=self.container,
            )
        if not self.key:
            raise ConfigurationError(
                f"{self.scheme} URI is missing an object key",
                parameter="key",
                value=self.key,
            )

    def __repr__(self) -> str:
        return f"ObjectLocation({self.scheme}://{self.container}/{self.key})"


class BaseRangeAdapter(ABC):
    """
    Turns a provider URI and a byte range into a signed, issuable request.

    Deliberately synchronous and free of I/O. Subclasses compute; they do not
    connect. See the module docstring for how this relates to the async
    streaming interface in the design documentation.
    """

    #: URI schemes this adapter claims.
    schemes: Tuple[str, ...] = ()

    @abstractmethod
    def parse_uri(self, uri: str) -> ObjectLocation:
        """
        Resolve a provider URI into its parts.

        Raises:
            ConfigurationError: If the URI is malformed or not this adapter's.
        """

    @abstractmethod
    def build_range_request(
        self, uri: str, start_byte: int, end_byte: int
    ) -> SignedRequest:
        """
        Produce an authenticated range request for part of an object.

        Raises:
            ConfigurationError: If the URI is malformed, the range is invalid,
                or credentials are unavailable.
        """

    def handles(self, uri: str) -> bool:
        """Whether this adapter claims the given URI's scheme."""
        if not isinstance(uri, str):
            return False
        lowered = uri.lower()
        return any(lowered.startswith(f"{scheme}://") for scheme in self.schemes)
