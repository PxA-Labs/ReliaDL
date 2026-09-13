"""
Azure Blob Storage authenticated range download adapter for ReliaDL.

Azure offers three unrelated ways to authorize the same read, and they differ in
where the credential lives rather than merely in how it is spelled:

* **Shared Key** signs the request with the storage account key, HMAC-SHA256
  over a fixed-layout string.
* **SAS** carries a pre-authorized signature in the query string; the request
  itself needs no Authorization header at all.
* **Bearer** presents an Entra ID token, exactly as GCS does.

The adapter picks whichever is configured and refuses to guess when more than
one is, because the failure mode of guessing is a 403 that names neither
credential.

Range headers
-------------
Azure accepts the standard ``Range`` header but defines its own ``x-ms-range``,
which takes precedence when both are present. This adapter sends ``x-ms-range``
by default because it is the documented spelling for the Blob service and is
what the ``x-ms-version`` contract covers.

The choice is not cosmetic under Shared Key, and this is the detail most easily
got wrong: the string to sign has a positional ``Range`` slot, and a range sent
as ``x-ms-range`` is signed through the canonicalized ``x-ms-*`` block instead,
leaving that slot *empty*. Putting the range in both places, or in the wrong
one, produces a signature Azure rejects with no indication which field differed.

The string to sign
------------------
Thirteen newline-separated positional fields, then the canonicalized headers and
resource:

    VERB / Content-Encoding / Content-Language / Content-Length / Content-MD5 /
    Content-Type / Date / If-Modified-Since / If-Match / If-None-Match /
    If-Unmodified-Since / Range / CanonicalizedHeaders / CanonicalizedResource

Every field is positional, so an omitted one is an empty line rather than an
absent line. ``Date`` is left empty whenever ``x-ms-date`` is sent, which it
always is here — Azure requires exactly one of the two to be authoritative, and
signing both invites a mismatch when an intermediary rewrites ``Date``.
``Content-Length`` is empty rather than ``0`` for a request with no body, which
is a rule that changed in API version 2015-02-21 and still trips older examples.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from src.adapters.base import (
    BaseRangeAdapter,
    ObjectLocation,
    SignedRequest,
    format_range_header,
)
from src.exceptions import ConfigurationError

# Blob service endpoint suffix for the public Azure cloud.
AZURE_BLOB_SUFFIX = "blob.core.windows.net"

# REST API version the request contract is pinned to. Pinning matters: the
# meaning of several signed fields has changed between versions, so a floating
# version would silently alter what a correct signature looks like.
AZURE_API_VERSION = "2023-11-03"

# Positional header slots in the string to sign, in their required order.
_SIGNED_HEADER_SLOTS: Tuple[str, ...] = (
    "Content-Encoding",
    "Content-Language",
    "Content-Length",
    "Content-MD5",
    "Content-Type",
    "Date",
    "If-Modified-Since",
    "If-Match",
    "If-None-Match",
    "If-Unmodified-Since",
    "Range",
)


class AzureAuthMode(str, Enum):
    """How a request is authorized."""

    # HMAC-SHA256 over the string to sign, using the account key.
    SHARED_KEY = "SHARED_KEY"

    # Pre-authorized signature carried in the query string.
    SAS = "SAS"

    # Entra ID / managed identity access token.
    BEARER = "BEARER"


def format_rfc1123(moment: datetime) -> str:
    """
    Render a timestamp in the RFC 1123 form Azure signs.

    Always GMT and always in English, which is why the month and day names are
    written out rather than taken from ``strftime`` — ``%a`` and ``%b`` follow
    the process locale, and a host running under a non-English locale would
    otherwise produce a date Azure cannot parse.

    Raises:
        ConfigurationError: If the timestamp is not timezone-aware.
    """
    if moment.tzinfo is None:
        raise ConfigurationError(
            "timestamp must be timezone-aware; a naive datetime would be signed "
            "as GMT regardless of the host's local zone and produce a signature "
            "Azure rejects as skewed",
            parameter="timestamp",
            value=moment,
        )
    utc = moment.astimezone(timezone.utc)
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return (
        f"{days[utc.weekday()]}, {utc.day:02d} {months[utc.month - 1]} "
        f"{utc.year:04d} {utc.hour:02d}:{utc.minute:02d}:{utc.second:02d} GMT"
    )


@dataclass(frozen=True)
class AzureSharedKeyCredential:
    """
    A storage account name and its base64-encoded shared key.

    Attributes:
        account_name: Storage account the key belongs to.
        account_key: Base64-encoded account key.
    """

    account_name: str
    account_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.account_name, str) or not self.account_name:
            raise ConfigurationError(
                "account_name must be a non-empty string",
                parameter="account_name",
                value=self.account_name,
            )
        if not isinstance(self.account_key, str) or not self.account_key:
            raise ConfigurationError(
                "account_key must be a non-empty string",
                parameter="account_key",
                value="<redacted>",
            )
        # Decoding here rather than at signing time turns a mistyped key into an
        # immediate configuration error instead of a 403 on the first chunk.
        try:
            base64.b64decode(self.account_key, validate=True)
        except Exception as error:  # noqa: BLE001 - surfaced as configuration
            raise ConfigurationError(
                f"account_key must be valid base64: {error}",
                parameter="account_key",
                value="<redacted>",
            ) from error

    @property
    def key_bytes(self) -> bytes:
        """The decoded signing key."""
        return base64.b64decode(self.account_key, validate=True)

    def __repr__(self) -> str:
        return (
            f"AzureSharedKeyCredential(account_name={self.account_name!r}, "
            "account_key=<redacted>)"
        )


class AzureSharedKeySigner:
    """
    Computes Azure Blob Shared Key signatures.

    Kept separate from the adapter so the string to sign can be asserted
    directly. A Shared Key failure gives back only a 403, so the intermediate
    document is the only thing that makes a mismatch diagnosable.
    """

    def __init__(self, credential: AzureSharedKeyCredential) -> None:
        if not isinstance(credential, AzureSharedKeyCredential):
            raise ConfigurationError(
                "credential must be an AzureSharedKeyCredential, got "
                f"{type(credential).__name__}",
                parameter="credential",
                value=type(credential).__name__,
            )
        self._credential = credential

    @property
    def account_name(self) -> str:
        """Storage account signatures are produced for."""
        return self._credential.account_name

    @staticmethod
    def canonicalized_headers(headers: Mapping[str, str]) -> str:
        """
        Canonicalize the ``x-ms-*`` headers.

        Lowercased, sorted by name, values trimmed, one per line. Only the
        ``x-ms-`` prefix participates; other headers are covered by their
        positional slots or not at all.
        """
        selected = {
            name.lower().strip(): " ".join(str(value).split())
            for name, value in headers.items()
            if name.lower().strip().startswith("x-ms-")
        }
        return "".join(f"{name}:{selected[name]}\n" for name in sorted(selected))

    @staticmethod
    def canonicalized_resource(account: str, path: str, query: str = "") -> str:
        """
        Canonicalize the resource path and any query parameters.

        Parameter names are lowercased and sorted, and repeated values are
        comma-joined in sorted order, so a signature cannot depend on the order
        a client happened to serialize them in.
        """
        resource = f"/{account}{path if path.startswith('/') else '/' + path}"
        if not query:
            return resource
        parsed = parse_qs(query, keep_blank_values=True)
        lines = []
        for name in sorted(key.lower() for key in parsed):
            values = ",".join(sorted(parsed[name]))
            lines.append(f"{name}:{values}")
        return resource + "\n" + "\n".join(lines)

    def string_to_sign(
        self,
        method: str,
        account: str,
        path: str,
        headers: Mapping[str, str],
        query: str = "",
    ) -> str:
        """
        Assemble the thirteen-field string to sign.

        Positional fields absent from the request contribute empty lines rather
        than being omitted, which is what keeps the layout fixed.
        """
        lookup = {name.lower(): str(value) for name, value in headers.items()}
        slots = []
        for slot in _SIGNED_HEADER_SLOTS:
            value = lookup.get(slot.lower(), "")
            if slot == "Content-Length" and value in ("0", ""):
                # Empty rather than zero since API version 2015-02-21.
                value = ""
            if slot == "Date" and "x-ms-date" in lookup:
                # Exactly one date is authoritative; signing both invites a
                # mismatch when an intermediary rewrites Date.
                value = ""
            slots.append(value)
        return "\n".join(
            [method.upper()]
            + slots
            + [
                self.canonicalized_headers(headers).rstrip("\n"),
                self.canonicalized_resource(account, path, query),
            ]
        )

    def signature(self, string_to_sign: str) -> str:
        """Base64-encoded HMAC-SHA256 of the string to sign."""
        digest = hmac.new(
            self._credential.key_bytes,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def authorization(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: str = "",
    ) -> str:
        """Build the complete Authorization header value."""
        document = self.string_to_sign(
            method=method,
            account=self._credential.account_name,
            path=path,
            headers=headers,
            query=query,
        )
        return f"SharedKey {self._credential.account_name}:{self.signature(document)}"

    def __repr__(self) -> str:
        return f"AzureSharedKeySigner(account={self._credential.account_name!r})"


class AzureBlobAdapter(BaseRangeAdapter):
    """
    Resolves ``az://`` references and authorizes Blob range reads.

    One of Shared Key, SAS or Bearer must be configured. Supplying more than one
    is refused rather than resolved by precedence: the failure mode of choosing
    wrongly is a 403 that names neither credential, which is far harder to
    diagnose than a configuration error raised up front.
    """

    schemes = ("az",)

    def __init__(
        self,
        account_name: Optional[str] = None,
        shared_key: Optional[AzureSharedKeyCredential] = None,
        sas_token: Optional[str] = None,
        access_token: Optional[str] = None,
        token_provider: Optional[Callable[[], str]] = None,
        endpoint_suffix: str = AZURE_BLOB_SUFFIX,
        api_version: str = AZURE_API_VERSION,
        use_x_ms_range: bool = True,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        configured = [
            name
            for name, value in (
                ("shared_key", shared_key),
                ("sas_token", sas_token),
                ("access_token", access_token or token_provider),
            )
            if value is not None
        ]
        if len(configured) > 1:
            raise ConfigurationError(
                f"Configure exactly one Azure credential, got {configured}. "
                "Choosing between them by precedence would fail with a 403 that "
                "names neither",
                parameter="credentials",
                value=configured,
            )
        if not configured:
            raise ConfigurationError(
                "An Azure credential is required: shared_key, sas_token, or an "
                "access_token/token_provider",
                parameter="credentials",
                value=None,
            )

        self._account_name = account_name or (
            shared_key.account_name if shared_key else None
        )
        self._shared_key = shared_key
        self._signer = AzureSharedKeySigner(shared_key) if shared_key else None
        self._sas_token = sas_token.lstrip("?") if sas_token else None
        self._endpoint_suffix = endpoint_suffix
        self._api_version = api_version
        self._use_x_ms_range = bool(use_x_ms_range)
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

        if token_provider is not None:
            self._token_provider: Optional[Callable[[], str]] = token_provider
        elif access_token is not None:
            self._token_provider = lambda: access_token
        else:
            self._token_provider = None

        self._mode = (
            AzureAuthMode.SHARED_KEY
            if shared_key
            else AzureAuthMode.SAS
            if self._sas_token
            else AzureAuthMode.BEARER
        )

    @property
    def auth_mode(self) -> AzureAuthMode:
        """How this adapter authorizes its requests."""
        return self._mode

    @property
    def account_name(self) -> Optional[str]:
        """Storage account, when known from configuration."""
        return self._account_name

    @property
    def api_version(self) -> str:
        """Blob REST API version requests are pinned to."""
        return self._api_version

    @property
    def range_header_name(self) -> str:
        """Header this adapter carries the byte range in."""
        return "x-ms-range" if self._use_x_ms_range else "Range"

    def parse_uri(self, uri: str) -> ObjectLocation:
        """
        Resolve an Azure Blob reference into a container and blob name.

        The ``az://`` form names no account, so the adapter's configured account
        supplies it; an HTTPS URL carries the account in its hostname.

        Raises:
            ConfigurationError: If the reference is malformed, not an Azure one,
                or names no account and none is configured.
        """
        if not isinstance(uri, str) or not uri:
            raise ConfigurationError(
                "uri must be a non-empty string", parameter="uri", value=uri
            )
        parts = urlsplit(uri)

        if parts.scheme == "az":
            if not self._account_name:
                raise ConfigurationError(
                    f"{uri!r} names no storage account and none is configured; "
                    "pass account_name or use the https:// form",
                    parameter="account_name",
                    value=None,
                )
            return ObjectLocation(
                scheme="az",
                container=parts.netloc,
                key=parts.path.lstrip("/"),
            )

        if parts.scheme in ("http", "https"):
            host = parts.hostname or ""
            if self._endpoint_suffix not in host:
                raise ConfigurationError(
                    f"{uri!r} is not an Azure Blob endpoint; host {host!r} is "
                    f"not a {self._endpoint_suffix} address",
                    parameter="uri",
                    value=uri,
                )
            account = host.split(".", 1)[0]
            trimmed = parts.path.lstrip("/")
            container, _, blob = trimmed.partition("/")
            return ObjectLocation(
                scheme="az",
                container=container,
                key=blob,
                region=account,
                endpoint=host,
            )

        raise ConfigurationError(
            f"Unsupported Azure URI scheme {parts.scheme!r} in {uri!r}; expected "
            "az, http, or https",
            parameter="uri",
            value=uri,
        )

    def _account_for(self, location: ObjectLocation) -> str:
        """Account a location belongs to, from the URL or configuration."""
        account = location.region or self._account_name
        if not account:
            raise ConfigurationError(
                "No storage account could be determined for the request",
                parameter="account_name",
                value=None,
            )
        return account

    def build_range_request(
        self, uri: str, start_byte: int, end_byte: int
    ) -> SignedRequest:
        """
        Produce an authorized range GET for part of a blob.

        Raises:
            ConfigurationError: If the URI is malformed, the range is invalid,
                or the configured credential cannot produce authorization.
        """
        range_value = format_range_header(start_byte, end_byte)
        location = self.parse_uri(uri)
        account = self._account_for(location)
        host = location.endpoint or f"{account}.{self._endpoint_suffix}"
        path = f"/{location.container}/{quote(location.key, safe='/')}"

        headers: Dict[str, str] = {
            "Host": host,
            "x-ms-version": self._api_version,
            "x-ms-date": format_rfc1123(self._clock()),
            self.range_header_name: range_value,
        }

        query = ""
        if self._mode is AzureAuthMode.SAS:
            # A SAS is itself the authorization; adding a header would be
            # ignored at best and conflict at worst.
            query = self._sas_token or ""
        elif self._mode is AzureAuthMode.BEARER:
            headers["Authorization"] = f"Bearer {self._bearer_token()}"
        else:
            assert self._signer is not None
            headers["Authorization"] = self._signer.authorization(
                method="GET", path=path, headers=headers, query=query
            )

        url = f"https://{host}{path}" + (f"?{query}" if query else "")
        return SignedRequest(
            method="GET",
            url=url,
            headers=headers,
            start_byte=start_byte,
            end_byte=end_byte,
        )

    def _bearer_token(self) -> str:
        """
        Obtain the current Entra ID access token.

        Raises:
            ConfigurationError: If the provider yields nothing.
        """
        if self._token_provider is None:
            raise ConfigurationError(
                "No Azure access token available",
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
        return token

    def __repr__(self) -> str:
        return (
            f"AzureBlobAdapter(account={self._account_name!r}, "
            f"mode={self._mode.value}, api_version={self._api_version!r})"
        )
