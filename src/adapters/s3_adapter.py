"""
AWS S3 authenticated range download adapter for ReliaDL.

Signs each chunk request with AWS Signature Version 4. A parallel download
issues one range request per chunk and every one is signed independently, so
signing sits on the hot path and has to be both correct and cheap.

Why sign at all rather than presign once
----------------------------------------
A single presigned URL would cover every range of an object, since the Range
header is not part of a presigned URL's signature. The reason ReliaDL signs per
request anyway is that a presigned URL is a bearer token with a fixed lifetime:
it cannot be revoked, it appears in logs and process listings, and a transfer
running longer than the expiry dies partway through with no way to renew. Header
signing keeps the credential out of the URL and lets a long transfer re-sign
indefinitely.

The signature
-------------
SigV4 is a chain of HMAC-SHA256 steps whose purpose is to prove possession of
the secret key without transmitting it, and to bind the signature to a specific
request, region, service and day:

    kDate    = HMAC("AWS4" + secret, date)
    kRegion  = HMAC(kDate, region)
    kService = HMAC(kRegion, service)
    kSigning = HMAC(kService, "aws4_request")
    signature = HexEncode(HMAC(kSigning, StringToSign))

The derivation is what makes the scheme tolerable to operate: the signing key
depends only on the day, region and service, so a compromised signing key is
useless the next day and against any other service, while the long-lived secret
never leaves the process.

The signature covers the canonical request, which includes the *Range* header.
That matters here more than it might elsewhere: a signature that did not cover
the range would let an intermediary rewrite which bytes were served, and a
transfer that verifies each chunk against a manifest hash would then fail
integrity checks for what looks like no reason.

Two S3-specific details that differ from other AWS services and are easy to get
wrong: the object key is URI-encoded once rather than twice, and the path is not
normalized, so a key containing ``.`` or ``..`` segments must be signed exactly
as written or the signature will not match what S3 computes.
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from src.adapters.base import (
    BaseRangeAdapter,
    ObjectLocation,
    SignedRequest,
    format_range_header,
)
from src.exceptions import ConfigurationError

# SigV4 algorithm identifier, carried in the Authorization header.
ALGORITHM = "AWS4-HMAC-SHA256"

# Terminator for the signing key derivation chain.
REQUEST_TYPE = "aws4_request"

# Service name S3 signs under.
S3_SERVICE = "s3"

# SHA-256 of the empty string. A range GET has no body, and S3 requires the
# payload hash to be signed, so this constant appears on every request.
EMPTY_PAYLOAD_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

# Region assumed when none is configured anywhere.
DEFAULT_REGION = "us-east-1"

# Characters RFC 3986 leaves unreserved. Everything else is percent-encoded.
_UNRESERVED = "-_.~"


def _uri_encode(value: str, encode_slash: bool = True) -> str:
    """
    Percent-encode a string per RFC 3986, as SigV4 canonicalization requires.

    Only A-Z, a-z, 0-9 and ``-_.~`` survive unencoded. ``/`` is preserved in
    paths and encoded in query values, which is the one place the two uses
    differ.
    """
    safe = _UNRESERVED if encode_slash else _UNRESERVED + "/"
    return quote(value, safe=safe)


def _sha256_hex(payload: bytes) -> str:
    """Hex-encoded SHA-256 digest."""
    return hashlib.sha256(payload).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    """HMAC-SHA256 of a UTF-8 message under a binary key."""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _normalize_header_value(value: str) -> str:
    """
    Trim and collapse whitespace in a header value.

    SigV4 canonicalization requires sequential spaces be folded to one and the
    value stripped, because intermediaries may legally reformat whitespace and
    a signature that depended on it would break in transit.
    """
    return " ".join(str(value).split())


@dataclass(frozen=True)
class AWSCredentials:
    """
    An AWS credential set.

    Attributes:
        access_key_id: Public identifier for the key.
        secret_access_key: Secret used to derive the signing key.
        session_token: Present for temporary credentials from STS, instance
            profiles, or assumed roles.
    """

    access_key_id: str
    secret_access_key: str
    session_token: Optional[str] = None

    def __post_init__(self) -> None:
        for name, value in (
            ("access_key_id", self.access_key_id),
            ("secret_access_key", self.secret_access_key),
        ):
            if not isinstance(value, str) or not value:
                raise ConfigurationError(
                    f"{name} must be a non-empty string",
                    parameter=name,
                    value="<redacted>" if "secret" in name else value,
                )

    @property
    def is_temporary(self) -> bool:
        """Whether these credentials carry a session token and will expire."""
        return bool(self.session_token)

    def __repr__(self) -> str:
        # The secret must never reach a log or a traceback. Exposing the key id
        # alone keeps the value diagnosable without being disclosive.
        return (
            f"AWSCredentials(access_key_id={self.access_key_id!r}, "
            f"secret_access_key=<redacted>, temporary={self.is_temporary})"
        )


def credentials_from_environment(
    environ: Optional[Mapping[str, str]] = None
) -> Optional[AWSCredentials]:
    """
    Read credentials from the standard AWS environment variables.

    Returns None when the variables are absent, so the resolution chain can
    continue rather than treating an unset environment as a failure.
    """
    source = os.environ if environ is None else environ
    access_key = source.get("AWS_ACCESS_KEY_ID")
    secret_key = source.get("AWS_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        return None
    return AWSCredentials(
        access_key_id=access_key,
        secret_access_key=secret_key,
        session_token=source.get("AWS_SESSION_TOKEN") or None,
    )


def credentials_from_shared_file(
    profile: str = "default", path: Optional[Path] = None
) -> Optional[AWSCredentials]:
    """
    Read credentials from the AWS shared credentials file.

    Returns None when the file or profile is absent. A malformed file raises,
    because a credentials file that exists but cannot be parsed is a
    misconfiguration the operator wants told about rather than silently skipped
    on the way to an anonymous request.
    """
    location = (
        Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials"))
        if path is None
        else path
    )
    location = location.expanduser()
    if not location.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(location)
    except configparser.Error as error:
        raise ConfigurationError(
            f"AWS shared credentials file at {location} could not be parsed: {error}",
            parameter="path",
            value=str(location),
        ) from error

    if not parser.has_section(profile):
        return None
    section = parser[profile]
    access_key = section.get("aws_access_key_id")
    secret_key = section.get("aws_secret_access_key")
    if not access_key or not secret_key:
        return None
    return AWSCredentials(
        access_key_id=access_key,
        secret_access_key=secret_key,
        session_token=section.get("aws_session_token") or None,
    )


def credentials_from_instance_metadata(
    fetcher: Callable[[], Mapping[str, str]]
) -> Optional[AWSCredentials]:
    """
    Build credentials from an instance profile or task role document.

    The fetcher is injected rather than performed here, because retrieving the
    document needs an HTTP client and a hop to a link-local address whose
    availability and timeout policy belong to the transport layer. Keeping it
    out means this module stays free of I/O and the chain stays unit-testable.

    Returns None when the fetcher yields nothing, matching the other providers.
    """
    document = fetcher()
    if not document:
        return None
    access_key = document.get("AccessKeyId")
    secret_key = document.get("SecretAccessKey")
    if not access_key or not secret_key:
        return None
    return AWSCredentials(
        access_key_id=access_key,
        secret_access_key=secret_key,
        session_token=document.get("Token") or None,
    )


def resolve_credentials(
    explicit: Optional[AWSCredentials] = None,
    environ: Optional[Mapping[str, str]] = None,
    profile: str = "default",
    shared_file: Optional[Path] = None,
    metadata_fetcher: Optional[Callable[[], Mapping[str, str]]] = None,
) -> AWSCredentials:
    """
    Resolve credentials in AWS's documented precedence order.

    Explicit configuration, then environment, then the shared credentials file,
    then instance metadata. The order runs cheapest and most specific first: an
    operator who passed keys directly means them, and the metadata hop is a
    network round trip worth avoiding whenever anything closer answered.

    Raises:
        ConfigurationError: If no provider yields credentials. Anonymous access
            is not a fallback — an unsigned request to a private bucket fails
            with a 403 that looks like a permissions problem rather than the
            missing-configuration problem it is.
    """
    if explicit is not None:
        return explicit

    from_env = credentials_from_environment(environ)
    if from_env is not None:
        return from_env

    from_file = credentials_from_shared_file(profile=profile, path=shared_file)
    if from_file is not None:
        return from_file

    if metadata_fetcher is not None:
        from_metadata = credentials_from_instance_metadata(metadata_fetcher)
        if from_metadata is not None:
            return from_metadata

    raise ConfigurationError(
        "No AWS credentials found. Checked explicit configuration, the "
        "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY environment variables, the "
        "shared credentials file, and instance metadata",
        parameter="credentials",
        value=None,
    )


@dataclass(frozen=True)
class CanonicalRequest:
    """
    The canonicalized request and the string derived from it.

    Both are retained because they are what one compares against AWS's own
    values when a signature does not match. A mismatched signature is otherwise
    undiagnosable: the failure is a single hex string with no indication of
    which of the six canonical components differed.

    Attributes:
        canonical_request: The canonical request document.
        string_to_sign: The string the signing key is applied to.
        signed_headers: Semicolon-joined lowercase header names that were signed.
        credential_scope: The date/region/service/terminator scope.
    """

    canonical_request: str
    string_to_sign: str
    signed_headers: str
    credential_scope: str


class SigV4Signer:
    """
    Computes AWS Signature Version 4 over a request description.

    Pure computation: given a method, URL, headers and payload hash it returns
    the headers to add. Keeping it separate from the S3 adapter is what allows
    it to be checked against AWS's published test vectors exactly, rather than
    against an endpoint policy that happens to agree with itself.
    """

    def __init__(
        self,
        credentials: AWSCredentials,
        region: str = DEFAULT_REGION,
        service: str = S3_SERVICE,
    ) -> None:
        if not isinstance(credentials, AWSCredentials):
            raise ConfigurationError(
                "credentials must be an AWSCredentials instance, got "
                f"{type(credentials).__name__}",
                parameter="credentials",
                value=type(credentials).__name__,
            )
        for name, value in (("region", region), ("service", service)):
            if not isinstance(value, str) or not value:
                raise ConfigurationError(
                    f"{name} must be a non-empty string",
                    parameter=name,
                    value=value,
                )
        self._credentials = credentials
        self._region = region
        self._service = service

    @property
    def region(self) -> str:
        """Region the signature is scoped to."""
        return self._region

    @property
    def service(self) -> str:
        """Service the signature is scoped to."""
        return self._service

    @property
    def encodes_path_twice(self) -> bool:
        """
        Whether path segments are URI-encoded twice during canonicalization.

        Every AWS service does except S3, which encodes once and does not
        normalize the path. Getting this wrong produces valid-looking signatures
        that S3 rejects for any key containing an encodable character.
        """
        return self._service != S3_SERVICE

    def canonical_uri(self, path: str) -> str:
        """Canonicalize a URL path for signing."""
        if not path:
            return "/"
        encoded = _uri_encode(path, encode_slash=False)
        if self.encodes_path_twice:
            encoded = _uri_encode(encoded, encode_slash=False)
        return encoded

    @staticmethod
    def canonical_query_string(query: str) -> str:
        """
        Canonicalize a query string: encoded, and sorted by name then value.

        Sorting is required because a signature must not depend on the order a
        client happened to serialize parameters in.
        """
        if not query:
            return ""
        pairs = []
        for name, values in parse_qs(query, keep_blank_values=True).items():
            for value in values:
                pairs.append((_uri_encode(name), _uri_encode(value)))
        pairs.sort()
        return "&".join(f"{name}={value}" for name, value in pairs)

    @staticmethod
    def canonical_headers(headers: Mapping[str, str]) -> Tuple[str, str]:
        """
        Canonicalize headers, returning the block and the signed-header list.

        Names are lowercased and sorted; values are trimmed and their internal
        whitespace collapsed.
        """
        normalized = {
            name.lower().strip(): _normalize_header_value(value)
            for name, value in headers.items()
        }
        ordered = sorted(normalized)
        block = "".join(f"{name}:{normalized[name]}\n" for name in ordered)
        return block, ";".join(ordered)

    def build_canonical_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload_hash: str,
        timestamp: datetime,
    ) -> CanonicalRequest:
        """Assemble the canonical request and the string to sign."""
        parts = urlsplit(url)
        canonical_headers, signed_headers = self.canonical_headers(headers)
        canonical_request = "\n".join(
            [
                method.upper(),
                self.canonical_uri(parts.path),
                self.canonical_query_string(parts.query),
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        datestamp = timestamp.strftime("%Y%m%d")
        scope = f"{datestamp}/{self._region}/{self._service}/{REQUEST_TYPE}"
        string_to_sign = "\n".join(
            [
                ALGORITHM,
                timestamp.strftime("%Y%m%dT%H%M%SZ"),
                scope,
                _sha256_hex(canonical_request.encode("utf-8")),
            ]
        )
        return CanonicalRequest(
            canonical_request=canonical_request,
            string_to_sign=string_to_sign,
            signed_headers=signed_headers,
            credential_scope=scope,
        )

    def signing_key(self, datestamp: str) -> bytes:
        """
        Derive the date-, region- and service-scoped signing key.

        The long-lived secret is used once, to seed the chain, and never
        transmitted. A leaked signing key is worthless the following day and
        against any other service or region.
        """
        key = _hmac(f"AWS4{self._credentials.secret_access_key}".encode("utf-8"), datestamp)
        key = _hmac(key, self._region)
        key = _hmac(key, self._service)
        return _hmac(key, REQUEST_TYPE)

    def signature(self, string_to_sign: str, datestamp: str) -> str:
        """Compute the hex signature for a string to sign."""
        return hmac.new(
            self.signing_key(datestamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def sign(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload_hash: str = EMPTY_PAYLOAD_SHA256,
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, str]:
        """
        Sign a request, returning the complete header set to send.

        The supplied headers are returned alongside the generated ones because
        the signature covers them: sending a subset, or adding a signed header
        afterwards, invalidates it.

        Raises:
            ConfigurationError: If the timestamp is not timezone-aware in UTC.
        """
        moment = datetime.now(timezone.utc) if timestamp is None else timestamp
        if moment.tzinfo is None:
            raise ConfigurationError(
                "timestamp must be timezone-aware; a naive datetime would be "
                "signed as UTC regardless of the host's local zone and produce "
                "a signature AWS rejects as skewed",
                parameter="timestamp",
                value=moment,
            )
        moment = moment.astimezone(timezone.utc)

        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        datestamp = moment.strftime("%Y%m%d")

        complete = dict(headers)
        complete.setdefault("x-amz-date", amz_date)
        complete.setdefault("x-amz-content-sha256", payload_hash)
        if self._credentials.session_token:
            # Signed, not merely sent: an unsigned token could be stripped or
            # swapped by an intermediary without invalidating the signature.
            complete.setdefault(
                "x-amz-security-token", self._credentials.session_token
            )

        canonical = self.build_canonical_request(
            method=method,
            url=url,
            headers=complete,
            payload_hash=payload_hash,
            timestamp=moment,
        )
        signature = self.signature(canonical.string_to_sign, datestamp)
        complete["Authorization"] = (
            f"{ALGORITHM} "
            f"Credential={self._credentials.access_key_id}/{canonical.credential_scope}, "
            f"SignedHeaders={canonical.signed_headers}, "
            f"Signature={signature}"
        )
        return complete

    def __repr__(self) -> str:
        return f"SigV4Signer(region={self._region!r}, service={self._service!r})"


# Suffix of the standard AWS S3 endpoint.
AWS_DOMAIN = "amazonaws.com"

# Query parameter that marks a URL as already presigned.
PRESIGNED_MARKER = "X-Amz-Signature"

# Longest and shortest legal S3 bucket names.
MIN_BUCKET_NAME = 3
MAX_BUCKET_NAME = 63


def is_dns_compatible_bucket(bucket: str) -> bool:
    """
    Whether a bucket name can be addressed virtual-hosted style over TLS.

    A name containing a dot is the case that matters. Such a bucket is legal and
    resolvable, but ``my.bucket.s3.amazonaws.com`` has three labels before the
    wildcard, and the ``*.s3.amazonaws.com`` certificate covers only one — so
    the request fails TLS verification rather than returning an S3 error. Those
    buckets must be addressed path-style instead.
    """
    if not MIN_BUCKET_NAME <= len(bucket) <= MAX_BUCKET_NAME:
        return False
    if "." in bucket or "_" in bucket:
        return False
    if bucket != bucket.lower():
        return False
    if bucket.startswith("-") or bucket.endswith("-"):
        return False
    return all(character.isalnum() or character == "-" for character in bucket)


class S3Adapter(BaseRangeAdapter):
    """
    Resolves ``s3://`` references and signs range requests against them.

    Holds no connection state, so a worker pool can share one instance. Each
    call re-signs from the current clock, which is what lets a transfer outlive
    any fixed credential lifetime.
    """

    schemes = ("s3",)

    def __init__(
        self,
        credentials: Optional[AWSCredentials] = None,
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        accelerate: bool = False,
        force_path_style: bool = False,
        environ: Optional[Mapping[str, str]] = None,
        profile: str = "default",
        shared_file: Optional[Path] = None,
        metadata_fetcher: Optional[Callable[[], Mapping[str, str]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        source = os.environ if environ is None else environ
        self._region = (
            region
            or source.get("AWS_REGION")
            or source.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION
        )
        if accelerate and endpoint:
            raise ConfigurationError(
                "accelerate and endpoint are mutually exclusive; Transfer "
                "Acceleration has its own endpoint and cannot be pointed at "
                "a custom one",
                parameter="accelerate",
                value=accelerate,
            )
        if accelerate and force_path_style:
            raise ConfigurationError(
                "Transfer Acceleration requires virtual-hosted addressing and "
                "cannot be combined with force_path_style",
                parameter="accelerate",
                value=accelerate,
            )

        self._endpoint = endpoint
        self._accelerate = bool(accelerate)
        self._force_path_style = bool(force_path_style)
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

        # Credentials are resolved once, at construction, so a misconfigured
        # environment fails immediately rather than on the first chunk of a
        # transfer the operator has already started waiting on.
        self._credentials = resolve_credentials(
            explicit=credentials,
            environ=environ,
            profile=profile,
            shared_file=shared_file,
            metadata_fetcher=metadata_fetcher,
        )
        self._signer = SigV4Signer(
            credentials=self._credentials, region=self._region, service=S3_SERVICE
        )

    @property
    def region(self) -> str:
        """Region requests are signed for."""
        return self._region

    @property
    def credentials(self) -> AWSCredentials:
        """Credentials this adapter signs with."""
        return self._credentials

    @property
    def signer(self) -> SigV4Signer:
        """Signer used for each request."""
        return self._signer

    def parse_uri(self, uri: str) -> ObjectLocation:
        """
        Resolve an S3 reference into a bucket and key.

        Accepts the ``s3://`` form, virtual-hosted HTTPS URLs, and path-style
        HTTPS URLs, since a manifest may carry any of the three.

        Raises:
            ConfigurationError: If the reference is malformed or not an S3 one.
        """
        if not isinstance(uri, str) or not uri:
            raise ConfigurationError(
                "uri must be a non-empty string", parameter="uri", value=uri
            )
        parts = urlsplit(uri)

        if parts.scheme == "s3":
            bucket = parts.netloc
            key = parts.path.lstrip("/")
            return ObjectLocation(scheme="s3", container=bucket, key=key)

        if parts.scheme in ("http", "https"):
            host = parts.hostname or ""
            if AWS_DOMAIN not in host:
                raise ConfigurationError(
                    f"{uri!r} is not an S3 endpoint; host {host!r} is not an "
                    f"{AWS_DOMAIN} address",
                    parameter="uri",
                    value=uri,
                )
            region = self._region_from_host(host)
            if host.startswith("s3.") or host.startswith("s3-"):
                # Path-style: the first path segment is the bucket.
                trimmed = parts.path.lstrip("/")
                bucket, _, key = trimmed.partition("/")
                return ObjectLocation(
                    scheme="s3",
                    container=bucket,
                    key=key,
                    region=region,
                    endpoint=host,
                )
            bucket = host.split(".s3", 1)[0]
            return ObjectLocation(
                scheme="s3",
                container=bucket,
                key=parts.path.lstrip("/"),
                region=region,
                endpoint=host,
            )

        raise ConfigurationError(
            f"Unsupported S3 URI scheme {parts.scheme!r} in {uri!r}; expected "
            "s3, http, or https",
            parameter="uri",
            value=uri,
        )

    @staticmethod
    def _region_from_host(host: str) -> Optional[str]:
        """
        Extract the region from an S3 hostname, if it carries one.

        Legacy global endpoints such as ``bucket.s3.amazonaws.com`` name no
        region and implicitly mean us-east-1; those return None so the adapter's
        configured region is used rather than a guess baked into the URL.
        """
        labels = host.split(".")
        for index, label in enumerate(labels):
            if label in ("s3", "s3-accelerate") or label.startswith("s3-"):
                remainder = labels[index + 1 :]
                if remainder and remainder[0] not in ("amazonaws", "dualstack"):
                    return remainder[0]
                return None
        return None

    def endpoint_for(self, location: ObjectLocation) -> Tuple[str, str]:
        """
        Resolve a location to the host and path a request should use.

        Virtual-hosted addressing is preferred, since path-style is deprecated
        for new buckets, but a name that is not DNS-compatible falls back to
        path-style automatically rather than producing a TLS failure the caller
        would have to diagnose.
        """
        if location.endpoint:
            host = location.endpoint
            if host.startswith("s3.") or host.startswith("s3-"):
                return host, f"/{location.container}/{location.key}"
            return host, f"/{location.key}"

        if self._endpoint:
            host = self._endpoint
            if self._force_path_style or not is_dns_compatible_bucket(
                location.container
            ):
                return host, f"/{location.container}/{location.key}"
            return f"{location.container}.{host}", f"/{location.key}"

        region = location.region or self._region
        if self._accelerate:
            return (
                f"{location.container}.s3-accelerate.{AWS_DOMAIN}",
                f"/{location.key}",
            )
        base = f"s3.{region}.{AWS_DOMAIN}"
        if self._force_path_style or not is_dns_compatible_bucket(location.container):
            return base, f"/{location.container}/{location.key}"
        return f"{location.container}.{base}", f"/{location.key}"

    @staticmethod
    def is_presigned(uri: str) -> bool:
        """Whether a URL already carries a SigV4 query signature."""
        parts = urlsplit(uri)
        if parts.scheme not in ("http", "https"):
            return False
        query = parse_qs(parts.query, keep_blank_values=True)
        return any(name.lower() == PRESIGNED_MARKER.lower() for name in query)

    def build_range_request(
        self, uri: str, start_byte: int, end_byte: int
    ) -> SignedRequest:
        """
        Produce a signed range GET for part of an S3 object.

        A presigned URL is passed through unsigned. Its signature was computed
        without the Range header — S3 does not sign Range for query-string auth —
        so adding one is legitimate, whereas re-signing would overwrite the
        caller's credential with ours and fail.

        Raises:
            ConfigurationError: If the URI is malformed or the range invalid.
        """
        range_header = format_range_header(start_byte, end_byte)

        if self.is_presigned(uri):
            return SignedRequest(
                method="GET",
                url=uri,
                headers={"Range": range_header},
                start_byte=start_byte,
                end_byte=end_byte,
            )

        location = self.parse_uri(uri)
        host, path = self.endpoint_for(location)
        url = f"https://{host}{_uri_encode(path, encode_slash=False)}"

        headers = self._signer.sign(
            method="GET",
            url=url,
            headers={"Host": host, "Range": range_header},
            payload_hash=EMPTY_PAYLOAD_SHA256,
            timestamp=self._clock(),
        )
        return SignedRequest(
            method="GET",
            url=url,
            headers=headers,
            start_byte=start_byte,
            end_byte=end_byte,
        )

    def __repr__(self) -> str:
        return (
            f"S3Adapter(region={self._region!r}, "
            f"accelerate={self._accelerate}, "
            f"key_id={self._credentials.access_key_id!r})"
        )
