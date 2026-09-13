"""
Google Cloud Storage authenticated range download adapter for ReliaDL.

GCS is the simplest of the three cloud adapters at request time: authentication
is a bearer token in a header, and byte ranges use the standard HTTP ``Range``
header with no provider-specific spelling. Nothing about the request is signed,
so a token is all that distinguishes an authorized read from a rejected one.

That simplicity moves the difficulty to obtaining the token, which is where the
work actually is.

Where the token comes from
--------------------------
A service account key cannot be sent to GCS. It signs a short-lived JWT
assertion which is exchanged at Google's OAuth2 endpoint for an access token,
and only that token is presented to storage. The exchange is an HTTP round trip,
so it belongs to the transport; what belongs here is building and signing the
assertion, which is pure computation over the key file and is the part that is
easy to get subtly wrong.

The split has a second benefit. Access tokens expire, typically within an hour,
while a large transfer may run for considerably longer. Resolving a token once
at construction would strand a transfer partway through with a 401 that looks
like a permissions change. The adapter therefore holds a *token provider*
callable and consults it per request, leaving refresh policy to whoever owns the
credential.

The assertion
-------------
    header  = {"alg": "RS256", "typ": "JWT", "kid": <private key id>}
    claims  = {"iss": <client email>, "scope": <storage scope>,
               "aud": <token endpoint>, "iat": now, "exp": now + lifetime}
    JWT     = b64url(header) + "." + b64url(claims) + "." + b64url(RSA-SHA256(...))

Each segment is base64url encoded *without padding*. That detail is load-bearing
rather than cosmetic: JWT is defined over base64url without ``=``, and a token
carrying padding is rejected by the exchange endpoint with an invalid_grant
error that says nothing about why.

The requested scope is read-only. A downloader has no reason to hold a token
that can write, and narrowing it means a leaked token cannot damage the bucket
it was issued against.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.adapters.base import (
    BaseRangeAdapter,
    ObjectLocation,
    SignedRequest,
    format_range_header,
)
from src.exceptions import ConfigurationError

# XML API host for object reads. Range requests are served directly from it.
GCS_HOST = "storage.googleapis.com"

# Google's OAuth2 token endpoint, the audience of the signed assertion.
GCS_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Read-only storage scope. A downloader never needs write access, and a narrow
# scope bounds the damage a leaked token can do.
GCS_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

# Lifetime requested for the assertion. Google caps this at an hour; asking for
# more is rejected rather than clamped.
DEFAULT_ASSERTION_LIFETIME = 3600

# Environment variable naming the service account key file.
CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

# Well-known location the gcloud CLI writes application default credentials to.
WELL_KNOWN_ADC = "~/.config/gcloud/application_default_credentials.json"


def _b64url(payload: bytes) -> str:
    """
    Base64url-encode without padding, as JWT requires.

    The stripped ``=`` is not cosmetic: an assertion carrying padding is
    rejected by the token endpoint with an invalid_grant error that gives no
    indication the encoding was the problem.
    """
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class GCSServiceAccount:
    """
    A service account key, loaded from its JSON key file.

    Attributes:
        client_email: Account identity, the assertion's issuer.
        private_key_pem: PEM-encoded RSA private key.
        private_key_id: Key identifier, carried in the JWT header so Google can
            select the right public key when several are active.
        token_uri: Endpoint the assertion is exchanged at.
        project_id: Owning project, when the key file names one.
    """

    client_email: str
    private_key_pem: str
    private_key_id: Optional[str] = None
    token_uri: str = GCS_TOKEN_URI
    project_id: Optional[str] = None

    def __post_init__(self) -> None:
        for name, value in (
            ("client_email", self.client_email),
            ("private_key_pem", self.private_key_pem),
        ):
            if not isinstance(value, str) or not value:
                raise ConfigurationError(
                    f"{name} must be a non-empty string",
                    parameter=name,
                    value="<redacted>" if "key" in name else value,
                )

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> "GCSServiceAccount":
        """
        Build from a parsed service account key document.

        Raises:
            ConfigurationError: If the document is not a service account key, or
                is missing a field the assertion needs.
        """
        account_type = document.get("type")
        if account_type != "service_account":
            raise ConfigurationError(
                f"Expected a service_account key document, got type={account_type!r}. "
                "Authorized-user credentials carry a refresh token rather than a "
                "signing key and cannot produce an assertion",
                parameter="type",
                value=account_type,
            )
        for field in ("client_email", "private_key"):
            if not document.get(field):
                raise ConfigurationError(
                    f"Service account key is missing required field {field!r}",
                    parameter=field,
                    value=None,
                )
        return cls(
            client_email=str(document["client_email"]),
            private_key_pem=str(document["private_key"]),
            private_key_id=(
                str(document["private_key_id"])
                if document.get("private_key_id")
                else None
            ),
            token_uri=str(document.get("token_uri") or GCS_TOKEN_URI),
            project_id=(
                str(document["project_id"]) if document.get("project_id") else None
            ),
        )

    @classmethod
    def from_file(cls, path: Path) -> "GCSServiceAccount":
        """
        Load a service account key from its JSON file.

        Raises:
            ConfigurationError: If the file is absent or not valid JSON.
        """
        location = Path(path).expanduser()
        if not location.is_file():
            raise ConfigurationError(
                f"Service account key file not found at {location}",
                parameter="path",
                value=str(location),
            )
        try:
            document = json.loads(location.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ConfigurationError(
                f"Service account key at {location} is not valid JSON: {error}",
                parameter="path",
                value=str(location),
            ) from error
        if not isinstance(document, dict):
            raise ConfigurationError(
                f"Service account key at {location} must contain a JSON object",
                parameter="path",
                value=str(location),
            )
        return cls.from_dict(document)

    def load_private_key(self) -> rsa.RSAPrivateKey:
        """
        Parse the PEM private key.

        Raises:
            ConfigurationError: If the key cannot be parsed or is not RSA.
        """
        try:
            key = serialization.load_pem_private_key(
                self.private_key_pem.encode("utf-8"), password=None
            )
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"Service account private key could not be parsed: {error}",
                parameter="private_key_pem",
                value="<redacted>",
            ) from error
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ConfigurationError(
                "Service account private key must be RSA; Google signs assertions "
                f"with RS256 and this key is {type(key).__name__}",
                parameter="private_key_pem",
                value="<redacted>",
            )
        return key

    def build_assertion(
        self,
        scope: str = GCS_READ_SCOPE,
        lifetime_seconds: int = DEFAULT_ASSERTION_LIFETIME,
        issued_at: Optional[int] = None,
    ) -> str:
        """
        Build and sign the JWT assertion to exchange for an access token.

        Raises:
            ConfigurationError: If the lifetime is not a positive integer within
                the hour Google permits.
        """
        if isinstance(lifetime_seconds, bool) or not isinstance(lifetime_seconds, int):
            raise ConfigurationError(
                "lifetime_seconds must be an integer, got "
                f"{type(lifetime_seconds).__name__}",
                parameter="lifetime_seconds",
                value=lifetime_seconds,
            )
        if not 0 < lifetime_seconds <= DEFAULT_ASSERTION_LIFETIME:
            raise ConfigurationError(
                f"lifetime_seconds must lie in (0, {DEFAULT_ASSERTION_LIFETIME}]; "
                "Google rejects a longer assertion rather than clamping it, got "
                f"{lifetime_seconds}",
                parameter="lifetime_seconds",
                value=lifetime_seconds,
            )

        now = int(time.time()) if issued_at is None else int(issued_at)
        header: Dict[str, str] = {"alg": "RS256", "typ": "JWT"}
        if self.private_key_id:
            header["kid"] = self.private_key_id
        claims = {
            "iss": self.client_email,
            "scope": scope,
            "aud": self.token_uri,
            "iat": now,
            "exp": now + lifetime_seconds,
        }
        # Separators without spaces keep the encoded segments compact and
        # deterministic, so an assertion is reproducible for a given instant.
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            for part in (header, claims)
        )
        signature = self.load_private_key().sign(
            signing_input.encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return f"{signing_input}.{_b64url(signature)}"

    def __repr__(self) -> str:
        return (
            f"GCSServiceAccount(client_email={self.client_email!r}, "
            f"private_key_pem=<redacted>, project_id={self.project_id!r})"
        )


def find_application_default_credentials(
    environ: Optional[Mapping[str, str]] = None,
    well_known: Optional[Path] = None,
) -> Optional[Path]:
    """
    Locate an application default credentials file.

    Checks the documented environment variable first, then the path the gcloud
    CLI writes. Returns None when neither exists, so a caller supplying a token
    directly is not forced to have a key file on disk.
    """
    source = os.environ if environ is None else environ
    explicit = source.get(CREDENTIALS_ENV)
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate
        raise ConfigurationError(
            f"{CREDENTIALS_ENV} points at {candidate}, which does not exist. "
            "A credentials path that is set but wrong is a misconfiguration "
            "rather than an absent credential",
            parameter=CREDENTIALS_ENV,
            value=str(candidate),
        )

    fallback = Path(WELL_KNOWN_ADC if well_known is None else well_known).expanduser()
    return fallback if fallback.is_file() else None


class GCSAdapter(BaseRangeAdapter):
    """
    Resolves ``gs://`` references and attaches OAuth2 authorization to reads.

    The token provider is consulted per request rather than once, so a transfer
    outliving an access token's lifetime refreshes transparently instead of
    failing partway through with a 401.
    """

    schemes = ("gs",)

    def __init__(
        self,
        access_token: Optional[str] = None,
        token_provider: Optional[Callable[[], str]] = None,
        service_account: Optional[GCSServiceAccount] = None,
        host: str = GCS_HOST,
    ) -> None:
        if sum(x is not None for x in (access_token, token_provider)) > 1:
            raise ConfigurationError(
                "Pass either access_token or token_provider, not both; a static "
                "token and a refreshing provider disagree about which is "
                "authoritative",
                parameter="token_provider",
                value=None,
            )
        if not host:
            raise ConfigurationError(
                "host must be a non-empty string", parameter="host", value=host
            )

        self._service_account = service_account
        self._host = host
        if token_provider is not None:
            self._token_provider: Optional[Callable[[], str]] = token_provider
        elif access_token is not None:
            if not isinstance(access_token, str) or not access_token:
                raise ConfigurationError(
                    "access_token must be a non-empty string",
                    parameter="access_token",
                    value="<redacted>",
                )
            self._token_provider = lambda: access_token
        else:
            self._token_provider = None

    @property
    def host(self) -> str:
        """Storage host reads are issued against."""
        return self._host

    @property
    def service_account(self) -> Optional[GCSServiceAccount]:
        """Service account whose key signs token assertions, if configured."""
        return self._service_account

    @property
    def has_token_source(self) -> bool:
        """Whether a token can be obtained without an exchange."""
        return self._token_provider is not None

    def parse_uri(self, uri: str) -> ObjectLocation:
        """
        Resolve a GCS reference into a bucket and object name.

        Raises:
            ConfigurationError: If the reference is malformed or not a GCS one.
        """
        if not isinstance(uri, str) or not uri:
            raise ConfigurationError(
                "uri must be a non-empty string", parameter="uri", value=uri
            )
        parts = urlsplit(uri)

        if parts.scheme == "gs":
            return ObjectLocation(
                scheme="gs", container=parts.netloc, key=parts.path.lstrip("/")
            )

        if parts.scheme in ("http", "https"):
            host = parts.hostname or ""
            if host != GCS_HOST and not host.endswith(f".{GCS_HOST}"):
                raise ConfigurationError(
                    f"{uri!r} is not a Google Cloud Storage endpoint; host "
                    f"{host!r} is not {GCS_HOST}",
                    parameter="uri",
                    value=uri,
                )
            trimmed = parts.path.lstrip("/")
            bucket, _, key = trimmed.partition("/")
            return ObjectLocation(
                scheme="gs", container=bucket, key=key, endpoint=host
            )

        raise ConfigurationError(
            f"Unsupported GCS URI scheme {parts.scheme!r} in {uri!r}; expected "
            "gs, http, or https",
            parameter="uri",
            value=uri,
        )

    def object_url(self, location: ObjectLocation) -> str:
        """
        Build the read URL for an object.

        The object name is percent-encoded but its slashes are preserved: GCS
        buckets are flat, and what looks like a directory separator is an
        ordinary character in the name that the XML API expects unencoded.
        """
        host = location.endpoint or self._host
        return f"https://{host}/{location.container}/{quote(location.key, safe='/')}"

    def _authorization(self) -> str:
        """
        Produce the bearer header value for the current request.

        Raises:
            ConfigurationError: If no token source is configured.
        """
        if self._token_provider is None:
            raise ConfigurationError(
                "No GCS access token available. Supply access_token, a "
                "token_provider, or exchange a service account assertion for a "
                "token and pass it in — GCS reads are rejected without one",
                parameter="token_provider",
                value=None,
            )
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise ConfigurationError(
                "token_provider returned an empty token",
                parameter="token_provider",
                value="<redacted>",
            )
        return f"Bearer {token}"

    def build_range_request(
        self, uri: str, start_byte: int, end_byte: int
    ) -> SignedRequest:
        """
        Produce an authorized range GET for part of a GCS object.

        Raises:
            ConfigurationError: If the URI is malformed, the range is invalid,
                or no token source is configured.
        """
        range_header = format_range_header(start_byte, end_byte)
        location = self.parse_uri(uri)
        url = self.object_url(location)
        return SignedRequest(
            method="GET",
            url=url,
            headers={
                "Host": location.endpoint or self._host,
                "Range": range_header,
                "Authorization": self._authorization(),
            },
            start_byte=start_byte,
            end_byte=end_byte,
        )

    def __repr__(self) -> str:
        return (
            f"GCSAdapter(host={self._host!r}, "
            f"token_source={'configured' if self.has_token_source else 'none'})"
        )
