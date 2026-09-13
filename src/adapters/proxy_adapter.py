"""
Enterprise forward proxy tunneling for ReliaDL: HTTP CONNECT and SOCKS5.

A corporate network rarely lets a downloader reach an origin directly. Both
protocols here solve the same problem — persuade an intermediary to open a raw
TCP conduit to somewhere else and then get out of the way — and both hand back
an ordinary socket that TLS can be negotiated over afterwards.

Unlike the cloud adapters in this package, this module performs I/O. It has to:
a tunnel is established by a handshake, not by describing one, and there is
nothing to sign or defer.

Why the range engine cares which one it gets
--------------------------------------------
Once a tunnel is open the two protocols are indistinguishable, so the rest of
the system never has to know. What differs is what the proxy learns and what it
can do about it. A CONNECT proxy is told the hostname and may refuse, log, or
MITM it; a SOCKS5 proxy given a literal address never learns the name at all.
That is a policy difference, not a performance one, and it is the reason both
exist here rather than one.

Remote DNS
----------
SOCKS5 can carry either a resolved address or a hostname the proxy resolves on
the client's behalf. Resolving remotely is the default here and matters for two
independent reasons. Internal hostnames frequently do not resolve outside the
perimeter at all, so resolving locally fails before a connection is ever
attempted. And a local lookup leaks the destination to whatever resolver the
host is using, which defeats much of the point of tunnelling in the first
place.

The distinction is the one the ``socks5`` and ``socks5h`` URL schemes encode,
and both are honoured.

Reading a handshake off a stream
--------------------------------
Two details here are the usual source of tunnels that work in testing and
corrupt data in production.

TCP is a byte stream, so a read asking for eight bytes may return three. Every
read in this module loops until it has what it asked for, or the peer closes.

More subtly, the CONNECT response ends at the first blank line and the tunnelled
stream begins on the very next byte. A proxy may legally send both in one
segment, so a reader that grabs a fixed-size buffer can swallow the first bytes
of the server's TLS ServerHello. Those bytes are then gone, and the failure
appears much later as a handshake error that looks like a certificate problem.
This implementation reads the response a byte at a time until the terminator,
which is slower by an amount that does not matter once per connection and is
correct by construction.
"""

from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

from src.exceptions import (
    ProxyAuthenticationError,
    ProxyConnectionError,
    ProxyError,
)

# Default seconds to wait on the proxy handshake. Generous relative to a LAN
# round trip, because an enterprise proxy may authenticate against a directory
# service before answering.
DEFAULT_TIMEOUT = 30.0

# Largest CONNECT response accepted before the proxy is considered broken.
# Bounded so a proxy that never sends a terminator cannot exhaust memory.
MAX_RESPONSE_BYTES = 64 * 1024

# SOCKS5 protocol constants (RFC 1928).
SOCKS_VERSION = 0x05
SOCKS_CMD_CONNECT = 0x01
SOCKS_RESERVED = 0x00
SOCKS_ATYP_IPV4 = 0x01
SOCKS_ATYP_DOMAIN = 0x03
SOCKS_ATYP_IPV6 = 0x04
SOCKS_AUTH_NONE = 0x00
SOCKS_AUTH_USERPASS = 0x02
SOCKS_AUTH_UNACCEPTABLE = 0xFF

# Username/password sub-negotiation (RFC 1929).
SOCKS_AUTH_VERSION = 0x01
SOCKS_AUTH_SUCCESS = 0x00

# RFC 1928 reply codes, mapped to something an operator can act on.
SOCKS_REPLIES = {
    0x00: "succeeded",
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}

# A domain name is length-prefixed with a single byte, so it cannot exceed this.
MAX_DOMAIN_LENGTH = 255


class ProxyType(str, Enum):
    """Which tunnelling protocol a proxy speaks."""

    # HTTP CONNECT: the proxy is told the hostname.
    HTTP = "HTTP"

    # SOCKS5 resolving the hostname locally before connecting.
    SOCKS5 = "SOCKS5"

    # SOCKS5 delegating name resolution to the proxy.
    SOCKS5H = "SOCKS5H"

    @property
    def resolves_remotely(self) -> bool:
        """
        Whether the destination hostname is resolved by the proxy.

        True for HTTP CONNECT, which names the host in the request line, and for
        socks5h. Plain socks5 resolves locally and sends a literal address.
        """
        return self is not ProxyType.SOCKS5


@dataclass(frozen=True)
class ProxyConfig:
    """
    Where the proxy is and how to authenticate to it.

    Attributes:
        proxy_type: Protocol the proxy speaks.
        host: Proxy hostname or address.
        port: Proxy port.
        username: Username, when the proxy requires authentication.
        password: Password accompanying the username.
    """

    proxy_type: ProxyType
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ProxyError(
                "Proxy host must be a non-empty string", proxy_url=None
            )
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ProxyError(
                f"Proxy port must be an integer, got {type(self.port).__name__}"
            )
        if not 1 <= self.port <= 65535:
            raise ProxyError(f"Proxy port must lie in [1, 65535], got {self.port}")
        if self.password is not None and self.username is None:
            raise ProxyError(
                "A proxy password was supplied without a username; both "
                "protocols authenticate with a pair"
            )

    @property
    def requires_authentication(self) -> bool:
        """Whether credentials were configured for this proxy."""
        return self.username is not None

    @property
    def address(self) -> Tuple[str, int]:
        """Proxy endpoint as a connect tuple."""
        return (self.host, self.port)

    @property
    def sanitized_url(self) -> str:
        """
        Proxy URL with any credentials removed, safe to log.

        Errors carry this rather than the configured URL, since a proxy URL is a
        natural place for a password to end up and an exception is a natural
        place for one to leak.
        """
        return f"{self.proxy_type.value.lower()}://{self.host}:{self.port}"

    @classmethod
    def from_url(cls, url: str) -> "ProxyConfig":
        """
        Parse a proxy URL such as ``socks5h://user:pass@proxy.corp:1080``.

        Credentials are percent-decoded, because a password containing ``@`` or
        ``:`` has to be encoded to survive the URL form and would otherwise be
        silently truncated at the wrong delimiter.

        Raises:
            ProxyError: If the scheme is unknown or the URL names no port.
        """
        if not isinstance(url, str) or not url:
            raise ProxyError("Proxy URL must be a non-empty string")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        mapping = {
            "http": ProxyType.HTTP,
            "https": ProxyType.HTTP,
            "socks5": ProxyType.SOCKS5,
            "socks5h": ProxyType.SOCKS5H,
        }
        if scheme not in mapping:
            raise ProxyError(
                f"Unsupported proxy scheme {parts.scheme!r}; expected one of "
                f"{sorted(mapping)}"
            )
        if not parts.hostname:
            raise ProxyError(f"Proxy URL {url!r} names no host")

        default_port = 1080 if scheme.startswith("socks") else 8080
        return cls(
            proxy_type=mapping[scheme],
            host=parts.hostname,
            port=parts.port or default_port,
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password else None,
        )

    def __repr__(self) -> str:
        # Credentials are deliberately absent; this appears in transfer logs.
        return (
            f"ProxyConfig(type={self.proxy_type.value}, host={self.host!r}, "
            f"port={self.port}, authenticated={self.requires_authentication})"
        )


def recv_exact(sock: socket.socket, count: int, proxy_url: Optional[str] = None) -> bytes:
    """
    Read exactly ``count`` bytes, looping until satisfied.

    TCP is a stream: a read asking for eight bytes may return three. Treating a
    short read as the whole reply is the classic way to produce a handshake that
    passes on loopback and fails across a real network.

    Raises:
        ProxyConnectionError: If the peer closes before enough bytes arrive.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        received = sock.recv(remaining)
        if not received:
            raise ProxyConnectionError(
                f"Proxy closed the connection after {count - remaining} of "
                f"{count} expected bytes",
                proxy_url=proxy_url,
            )
        chunks.append(received)
        remaining -= len(received)
    return b"".join(chunks)


def read_until_blank_line(
    sock: socket.socket,
    limit: int = MAX_RESPONSE_BYTES,
    proxy_url: Optional[str] = None,
) -> bytes:
    """
    Read a CONNECT response up to and including its terminating blank line.

    Deliberately one byte at a time. The tunnelled stream begins on the byte
    after the terminator, and a proxy may send the response and the first
    payload bytes in a single segment, so a buffered read can swallow the start
    of the server's TLS ServerHello. Those bytes cannot be recovered, and the
    failure surfaces much later as a handshake error resembling a certificate
    problem.

    Raises:
        ProxyConnectionError: If the peer closes early or the response exceeds
            the limit without terminating.
    """
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        byte = sock.recv(1)
        if not byte:
            raise ProxyConnectionError(
                "Proxy closed the connection before completing its CONNECT "
                "response",
                proxy_url=proxy_url,
            )
        buffer += byte
        if len(buffer) > limit:
            raise ProxyConnectionError(
                f"Proxy CONNECT response exceeded {limit} bytes without a "
                "terminating blank line",
                proxy_url=proxy_url,
            )
    return bytes(buffer)


def _basic_credentials(username: str, password: Optional[str]) -> str:
    """Encode credentials for a Proxy-Authorization Basic challenge."""
    raw = f"{username}:{password or ''}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def open_http_connect_tunnel(
    config: ProxyConfig,
    target_host: str,
    target_port: int,
    timeout: float = DEFAULT_TIMEOUT,
    sock: Optional[socket.socket] = None,
) -> socket.socket:
    """
    Establish an HTTP CONNECT tunnel and return the connected socket.

    Credentials are sent pre-emptively rather than waiting for a 407 challenge.
    The extra round trip buys nothing when the proxy is known to require them,
    and a download opening many connections pays it on every one.

    Raises:
        ProxyAuthenticationError: If the proxy rejects the credentials (407).
        ProxyConnectionError: If the proxy is unreachable or refuses the tunnel.
    """
    url = config.sanitized_url
    connection = sock if sock is not None else _connect(config, timeout)

    authority = _format_authority(target_host, target_port)
    lines = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        # Some proxies close an idle tunnel without this, mid-transfer.
        "Proxy-Connection: keep-alive",
    ]
    if config.requires_authentication:
        token = _basic_credentials(config.username or "", config.password)
        lines.append(f"Proxy-Authorization: Basic {token}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    try:
        connection.sendall(request)
        response = read_until_blank_line(connection, proxy_url=url)
    except ProxyError:
        connection.close()
        raise
    except OSError as error:
        connection.close()
        raise ProxyConnectionError(
            f"CONNECT exchange with proxy failed: {error}", proxy_url=url
        ) from error

    status, reason = _parse_status_line(response, url)
    if status == 407:
        connection.close()
        raise ProxyAuthenticationError(
            f"Proxy requires authentication (407 {reason})", proxy_url=url
        )
    if not 200 <= status < 300:
        connection.close()
        raise ProxyConnectionError(
            f"Proxy refused the tunnel to {authority}: {status} {reason}",
            proxy_url=url,
        )
    return connection


def _parse_status_line(response: bytes, proxy_url: str) -> Tuple[int, str]:
    """
    Extract the status code and reason from a CONNECT response.

    Raises:
        ProxyConnectionError: If the response is not a recognizable status line.
    """
    first_line = response.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    fields = first_line.split(" ", 2)
    if len(fields) < 2 or not fields[0].upper().startswith("HTTP/"):
        raise ProxyConnectionError(
            f"Proxy sent a malformed CONNECT response: {first_line!r}",
            proxy_url=proxy_url,
        )
    try:
        status = int(fields[1])
    except ValueError as error:
        raise ProxyConnectionError(
            f"Proxy sent a non-numeric status code: {first_line!r}",
            proxy_url=proxy_url,
        ) from error
    return status, fields[2] if len(fields) > 2 else ""


def _format_authority(host: str, port: int) -> str:
    """
    Render a host and port as an HTTP authority.

    A literal IPv6 address is bracketed, without which the colons in the address
    are indistinguishable from the port separator.
    """
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def open_socks5_tunnel(
    config: ProxyConfig,
    target_host: str,
    target_port: int,
    timeout: float = DEFAULT_TIMEOUT,
    sock: Optional[socket.socket] = None,
) -> socket.socket:
    """
    Perform the RFC 1928 handshake and return the tunnelled socket.

    Three exchanges: a method greeting, optional RFC 1929 username/password
    sub-negotiation, and the CONNECT request itself.

    Raises:
        ProxyAuthenticationError: If the proxy rejects the credentials or offers
            no method the client can satisfy.
        ProxyConnectionError: If the handshake fails or the proxy refuses.
    """
    url = config.sanitized_url
    connection = sock if sock is not None else _connect(config, timeout)

    try:
        _socks5_negotiate_method(connection, config, url)
        _socks5_request_connect(connection, config, target_host, target_port, url)
    except ProxyError:
        connection.close()
        raise
    except OSError as error:
        connection.close()
        raise ProxyConnectionError(
            f"SOCKS5 handshake failed: {error}", proxy_url=url
        ) from error
    return connection


def _socks5_negotiate_method(
    connection: socket.socket, config: ProxyConfig, url: str
) -> None:
    """
    Agree an authentication method, then satisfy it.

    Offers username/password only when credentials exist. Advertising a method
    that cannot be completed invites the proxy to select it and then fail the
    sub-negotiation, which reports as a credential error rather than the absent
    configuration it is.
    """
    methods = [SOCKS_AUTH_NONE]
    if config.requires_authentication:
        methods.insert(0, SOCKS_AUTH_USERPASS)

    connection.sendall(bytes([SOCKS_VERSION, len(methods)]) + bytes(methods))
    version, method = recv_exact(connection, 2, url)

    if version != SOCKS_VERSION:
        raise ProxyConnectionError(
            f"Proxy replied with SOCKS version {version}, expected "
            f"{SOCKS_VERSION}",
            proxy_url=url,
        )
    if method == SOCKS_AUTH_UNACCEPTABLE:
        raise ProxyAuthenticationError(
            "Proxy accepted none of the offered authentication methods"
            + ("" if config.requires_authentication else "; no credentials were configured"),
            proxy_url=url,
        )
    if method == SOCKS_AUTH_NONE:
        return
    if method != SOCKS_AUTH_USERPASS:
        raise ProxyConnectionError(
            f"Proxy selected unsupported authentication method {method:#04x}",
            proxy_url=url,
        )
    _socks5_authenticate(connection, config, url)


def _socks5_authenticate(
    connection: socket.socket, config: ProxyConfig, url: str
) -> None:
    """
    Complete the RFC 1929 username/password sub-negotiation.

    Both fields are length-prefixed with a single byte, so neither may exceed
    255 bytes once encoded. The check is explicit because the alternative is a
    silently truncated credential and an authentication failure that looks like
    a wrong password.
    """
    if not config.requires_authentication:
        raise ProxyAuthenticationError(
            "Proxy requested username/password authentication but no "
            "credentials were configured",
            proxy_url=url,
        )
    username = (config.username or "").encode("utf-8")
    password = (config.password or "").encode("utf-8")
    for name, value in (("username", username), ("password", password)):
        if len(value) > 255:
            raise ProxyAuthenticationError(
                f"SOCKS5 {name} must encode to at most 255 bytes, got "
                f"{len(value)}",
                proxy_url=url,
            )

    connection.sendall(
        bytes([SOCKS_AUTH_VERSION, len(username)])
        + username
        + bytes([len(password)])
        + password
    )
    version, status = recv_exact(connection, 2, url)
    if version != SOCKS_AUTH_VERSION:
        raise ProxyConnectionError(
            f"Proxy replied with auth sub-negotiation version {version}, "
            f"expected {SOCKS_AUTH_VERSION}",
            proxy_url=url,
        )
    if status != SOCKS_AUTH_SUCCESS:
        raise ProxyAuthenticationError(
            f"Proxy rejected the supplied credentials (status {status:#04x})",
            proxy_url=url,
        )


def encode_socks5_address(host: str, port: int, resolve_remotely: bool) -> bytes:
    """
    Encode a destination as an address type, address and port.

    A literal address is always sent as its own type even when remote
    resolution is requested, since there is no name for the proxy to resolve.
    Otherwise the choice is the one that separates socks5 from socks5h.

    Raises:
        ProxyError: If a hostname is too long to length-prefix, or local
            resolution was requested and failed.
    """
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None

    if parsed is not None:
        atyp = SOCKS_ATYP_IPV4 if parsed.version == 4 else SOCKS_ATYP_IPV6
        return bytes([atyp]) + parsed.packed + port.to_bytes(2, "big")

    if resolve_remotely:
        encoded = host.encode("idna") if host.isascii() is False else host.encode("ascii")
        if len(encoded) > MAX_DOMAIN_LENGTH:
            raise ProxyError(
                f"Hostname is {len(encoded)} bytes; SOCKS5 length-prefixes a "
                f"domain with one byte and cannot carry more than "
                f"{MAX_DOMAIN_LENGTH}"
            )
        return (
            bytes([SOCKS_ATYP_DOMAIN, len(encoded)])
            + encoded
            + port.to_bytes(2, "big")
        )

    try:
        resolved = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
    except (socket.gaierror, IndexError) as error:
        raise ProxyError(
            f"Local resolution of {host!r} failed: {error}. An internal name "
            "that only resolves inside the perimeter needs socks5h, which "
            "leaves resolution to the proxy"
        ) from error
    address = ipaddress.ip_address(resolved)
    atyp = SOCKS_ATYP_IPV4 if address.version == 4 else SOCKS_ATYP_IPV6
    return bytes([atyp]) + address.packed + port.to_bytes(2, "big")


def _socks5_request_connect(
    connection: socket.socket,
    config: ProxyConfig,
    target_host: str,
    target_port: int,
    url: str,
) -> None:
    """
    Issue the CONNECT command and consume the reply in full.

    The bound address in the reply is discarded, but it must still be read: its
    length is variable and anything left unread would be delivered as the first
    bytes of the tunnelled stream, corrupting the TLS handshake that follows.
    """
    destination = encode_socks5_address(
        target_host, target_port, config.proxy_type.resolves_remotely
    )
    connection.sendall(
        bytes([SOCKS_VERSION, SOCKS_CMD_CONNECT, SOCKS_RESERVED]) + destination
    )

    version, reply, _reserved, atyp = recv_exact(connection, 4, url)
    if version != SOCKS_VERSION:
        raise ProxyConnectionError(
            f"Proxy replied with SOCKS version {version}, expected "
            f"{SOCKS_VERSION}",
            proxy_url=url,
        )
    if reply != 0x00:
        raise ProxyConnectionError(
            f"Proxy refused the tunnel to {target_host}:{target_port}: "
            f"{SOCKS_REPLIES.get(reply, f'unknown reply {reply:#04x}')}",
            proxy_url=url,
        )

    if atyp == SOCKS_ATYP_IPV4:
        recv_exact(connection, 4, url)
    elif atyp == SOCKS_ATYP_IPV6:
        recv_exact(connection, 16, url)
    elif atyp == SOCKS_ATYP_DOMAIN:
        length = recv_exact(connection, 1, url)[0]
        recv_exact(connection, length, url)
    else:
        raise ProxyConnectionError(
            f"Proxy replied with unknown address type {atyp:#04x}; the bound "
            "address cannot be consumed and the stream would be corrupt",
            proxy_url=url,
        )
    recv_exact(connection, 2, url)


def _connect(config: ProxyConfig, timeout: float) -> socket.socket:
    """
    Open a TCP connection to the proxy itself.

    Raises:
        ProxyConnectionError: If the proxy cannot be reached.
    """
    try:
        connection = socket.create_connection(config.address, timeout=timeout)
    except OSError as error:
        raise ProxyConnectionError(
            f"Could not reach proxy at {config.host}:{config.port}: {error}",
            proxy_url=config.sanitized_url,
        ) from error
    # Handshakes are tiny and strictly request/response, so Nagle only adds
    # latency to every exchange.
    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return connection


def build_ssl_context(
    ca_bundle: Optional[str] = None,
    verify: bool = True,
) -> ssl.SSLContext:
    """
    Build a TLS context, optionally trusting a corporate CA bundle.

    A TLS-inspecting proxy re-signs traffic with an internal authority that is
    not in the system trust store, so a custom bundle is the difference between
    working and a certificate error on every connection.

    ``verify=False`` disables both certificate and hostname checking and makes
    the connection trivially interceptable. It exists for diagnosing a proxy
    whose bundle is not yet available, and the warning belongs here because the
    option looks innocuous at the call site.

    Raises:
        ProxyError: If the bundle cannot be read.
    """
    context = ssl.create_default_context()
    if ca_bundle is not None:
        try:
            context.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as error:
            raise ProxyError(
                f"CA bundle at {ca_bundle!r} could not be loaded: {error}"
            ) from error
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class ProxyTunnel:
    """
    Opens tunnelled connections through a forward proxy.

    Holds configuration only; each call returns an independent socket, so a
    worker pool can share one instance without coordinating.
    """

    def __init__(
        self,
        config: ProxyConfig,
        timeout: float = DEFAULT_TIMEOUT,
        ca_bundle: Optional[str] = None,
        verify: bool = True,
    ) -> None:
        if not isinstance(config, ProxyConfig):
            raise ProxyError(
                f"config must be a ProxyConfig, got {type(config).__name__}"
            )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ProxyError(
                f"timeout must be numeric, got {type(timeout).__name__}"
            )
        if timeout <= 0:
            raise ProxyError(f"timeout must be positive, got {timeout}")

        self._config = config
        self._timeout = float(timeout)
        self._ca_bundle = ca_bundle
        self._verify = bool(verify)

    @property
    def config(self) -> ProxyConfig:
        """Proxy this tunnel connects through."""
        return self._config

    @property
    def timeout(self) -> float:
        """Seconds allowed for the handshake."""
        return self._timeout

    @property
    def ca_bundle(self) -> Optional[str]:
        """Corporate CA bundle trusted for TLS, if configured."""
        return self._ca_bundle

    def open(
        self,
        target_host: str,
        target_port: int,
        sock: Optional[socket.socket] = None,
    ) -> socket.socket:
        """
        Open a plain tunnel to a destination.

        Raises:
            ProxyAuthenticationError: If the proxy rejects the credentials.
            ProxyConnectionError: If the proxy is unreachable or refuses.
        """
        if not isinstance(target_host, str) or not target_host:
            raise ProxyError("target_host must be a non-empty string")
        if isinstance(target_port, bool) or not isinstance(target_port, int):
            raise ProxyError(
                f"target_port must be an integer, got {type(target_port).__name__}"
            )
        if not 1 <= target_port <= 65535:
            raise ProxyError(
                f"target_port must lie in [1, 65535], got {target_port}"
            )

        opener = (
            open_http_connect_tunnel
            if self._config.proxy_type is ProxyType.HTTP
            else open_socks5_tunnel
        )
        return opener(
            self._config, target_host, target_port, self._timeout, sock
        )

    def open_tls(
        self,
        target_host: str,
        target_port: int = 443,
        sock: Optional[socket.socket] = None,
    ) -> ssl.SSLSocket:
        """
        Open a tunnel and negotiate TLS with the destination through it.

        SNI and certificate verification target the destination, never the
        proxy. Verifying against the proxy's name would accept any certificate
        the proxy could obtain for itself, which is precisely the interception
        the verification exists to detect.

        Raises:
            ProxyError: If the TLS handshake fails through the tunnel.
        """
        tunnel = self.open(target_host, target_port, sock=sock)
        context = build_ssl_context(self._ca_bundle, self._verify)
        try:
            return context.wrap_socket(tunnel, server_hostname=target_host)
        except (ssl.SSLError, OSError) as error:
            tunnel.close()
            raise ProxyError(
                f"TLS handshake with {target_host}:{target_port} failed through "
                f"the tunnel: {error}",
                proxy_url=self._config.sanitized_url,
            ) from error

    def __repr__(self) -> str:
        return (
            f"ProxyTunnel(config={self._config!r}, timeout={self._timeout}, "
            f"verify={self._verify})"
        )
