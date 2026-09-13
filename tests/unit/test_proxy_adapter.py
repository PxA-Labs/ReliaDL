"""
Unit tests for HTTP CONNECT and SOCKS5 tunneling in
src.adapters.proxy_adapter. Exercised against in-process mock proxy servers
that speak the real wire protocols, covering the CONNECT exchange and its
failure codes, the RFC 1928 handshake with RFC 1929 authentication, remote
versus local name resolution, and the two stream-framing details that decide
whether a tunnel carries bytes intact.
"""

from __future__ import annotations

import socket
import ssl
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, List, Optional

import src.adapters.proxy_adapter as proxy_module
from src.adapters.proxy_adapter import (
    DEFAULT_TIMEOUT,
    SOCKS_ATYP_DOMAIN,
    SOCKS_ATYP_IPV4,
    SOCKS_ATYP_IPV6,
    ProxyConfig,
    ProxyTunnel,
    ProxyType,
    build_ssl_context,
    encode_socks5_address,
    open_http_connect_tunnel,
    open_socks5_tunnel,
    read_until_blank_line,
    recv_exact,
)
from src.exceptions import (
    ProxyAuthenticationError,
    ProxyConnectionError,
    ProxyError,
)


class MockProxy:
    """
    A single-connection proxy that speaks a real wire protocol.

    Runs in a thread on an ephemeral loopback port. The handler receives the
    accepted socket and drives whichever exchange the test needs, so the code
    under test performs genuine socket I/O rather than talking to a stub.
    """

    def __init__(self, handler: Callable[[socket.socket, "MockProxy"], None]) -> None:
        self._handler = handler
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.received: List[bytes] = []
        self.error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                self._handler(connection, self)
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            self.error = error

    def __enter__(self) -> "MockProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._thread.join(timeout=5)
        self._listener.close()

    def config(self, proxy_type: ProxyType, **kwargs) -> ProxyConfig:
        """A ProxyConfig pointing at this mock."""
        return ProxyConfig(
            proxy_type=proxy_type, host="127.0.0.1", port=self.port, **kwargs
        )


def read_request(connection: socket.socket) -> bytes:
    """Read a CONNECT request up to its terminating blank line."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        byte = connection.recv(1)
        if not byte:
            break
        buffer += byte
    return bytes(buffer)


def http_handler(
    status: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n",
    payload: bytes = b"",
    combine: bool = False,
) -> Callable[[socket.socket, MockProxy], None]:
    """
    Build a CONNECT handler returning a given status, then optional payload.

    ``combine`` sends the status and the payload in one segment, which is the
    legal behaviour that breaks a buffered response reader.
    """

    def handle(connection: socket.socket, server: MockProxy) -> None:
        server.received.append(read_request(connection))
        if combine:
            connection.sendall(status + payload)
        else:
            connection.sendall(status)
            if payload:
                connection.sendall(payload)

    return handle


def socks5_handler(
    method: int = 0x00,
    auth_status: int = 0x00,
    reply: int = 0x00,
    bnd_atyp: int = SOCKS_ATYP_IPV4,
    payload: bytes = b"",
) -> Callable[[socket.socket, MockProxy], None]:
    """Build a SOCKS5 handler that completes (or fails) the RFC 1928 exchange."""

    def handle(connection: socket.socket, server: MockProxy) -> None:
        version, count = recv_exact(connection, 2)
        offered = recv_exact(connection, count)
        server.received.append(bytes([version, count]) + offered)
        connection.sendall(bytes([0x05, method]))
        if method == 0xFF:
            return

        if method == 0x02:
            auth_version, ulen = recv_exact(connection, 2)
            username = recv_exact(connection, ulen)
            plen = recv_exact(connection, 1)[0]
            password = recv_exact(connection, plen)
            server.received.append(
                bytes([auth_version, ulen]) + username + bytes([plen]) + password
            )
            connection.sendall(bytes([0x01, auth_status]))
            if auth_status != 0x00:
                return

        header = recv_exact(connection, 4)
        atyp = header[3]
        if atyp == SOCKS_ATYP_IPV4:
            address = recv_exact(connection, 4)
        elif atyp == SOCKS_ATYP_IPV6:
            address = recv_exact(connection, 16)
        else:
            length = recv_exact(connection, 1)[0]
            address = bytes([length]) + recv_exact(connection, length)
        port = recv_exact(connection, 2)
        server.received.append(header + address + port)

        if bnd_atyp == SOCKS_ATYP_DOMAIN:
            bound = bytes([SOCKS_ATYP_DOMAIN, 9]) + b"proxy.lan"
        elif bnd_atyp == SOCKS_ATYP_IPV6:
            bound = bytes([SOCKS_ATYP_IPV6]) + bytes(16)
        else:
            bound = bytes([SOCKS_ATYP_IPV4]) + bytes(4)
        connection.sendall(bytes([0x05, reply, 0x00]) + bound + b"\x00\x00")
        if reply == 0x00 and payload:
            connection.sendall(payload)

    return handle


class TestProxyConfig(unittest.TestCase):
    """Proxy URLs, credentials, and the things that must never be logged."""

    def test_socks5h_selects_remote_resolution(self) -> None:
        config = ProxyConfig.from_url("socks5h://proxy.corp:1080")
        self.assertIs(config.proxy_type, ProxyType.SOCKS5H)
        self.assertTrue(config.proxy_type.resolves_remotely)

    def test_socks5_resolves_locally(self) -> None:
        self.assertFalse(ProxyType.SOCKS5.resolves_remotely)

    def test_http_connect_always_names_the_host(self) -> None:
        self.assertTrue(ProxyType.HTTP.resolves_remotely)

    def test_default_ports(self) -> None:
        self.assertEqual(ProxyConfig.from_url("socks5://p").port, 1080)
        self.assertEqual(ProxyConfig.from_url("http://p").port, 8080)

    def test_credentials_are_percent_decoded(self) -> None:
        """
        A password containing '@' or ':' must be encoded to survive the URL.

        Decoding it wrongly truncates at the wrong delimiter and presents a
        different password than the operator configured.
        """
        config = ProxyConfig.from_url("socks5h://user%40corp:p%40ss%3Aword@h:1080")
        self.assertEqual(config.username, "user@corp")
        self.assertEqual(config.password, "p@ss:word")

    def test_unsupported_scheme_rejected(self) -> None:
        for url in ("ftp://p:1080", "socks4://p:1080", ""):
            with self.assertRaises(ProxyError):
                ProxyConfig.from_url(url)

    def test_missing_host_rejected(self) -> None:
        with self.assertRaises(ProxyError):
            ProxyConfig.from_url("socks5://")

    def test_invalid_port_rejected(self) -> None:
        for port in (0, 65536, -1):
            with self.assertRaises(ProxyError):
                ProxyConfig(ProxyType.HTTP, "p", port)
        for bad in (80.0, "80", True):
            with self.assertRaises(ProxyError):
                ProxyConfig(ProxyType.HTTP, "p", bad)  # type: ignore[arg-type]

    def test_password_without_username_rejected(self) -> None:
        with self.assertRaises(ProxyError):
            ProxyConfig(ProxyType.HTTP, "p", 8080, password="secret")

    def test_empty_host_rejected(self) -> None:
        with self.assertRaises(ProxyError):
            ProxyConfig(ProxyType.HTTP, "", 8080)

    def test_credentials_never_appear_in_repr_or_sanitized_url(self) -> None:
        """A proxy URL is a natural place for a password to end up."""
        config = ProxyConfig.from_url("socks5h://user:hunter2@proxy.corp:1080")
        self.assertNotIn("hunter2", repr(config))
        self.assertNotIn("hunter2", config.sanitized_url)
        self.assertNotIn("user", config.sanitized_url)
        self.assertEqual(config.sanitized_url, "socks5h://proxy.corp:1080")


class TestStreamFraming(unittest.TestCase):
    """The two reads that decide whether a tunnel carries bytes intact."""

    def test_recv_exact_reassembles_short_reads(self) -> None:
        """
        TCP may satisfy a read for eight bytes with three.

        Treating a short read as the whole reply is the classic way to build a
        handshake that passes on loopback and fails across a real network.
        """
        left, right = socket.socketpair()
        with left, right:

            def dribble() -> None:
                for byte in b"12345678":
                    right.sendall(bytes([byte]))

            thread = threading.Thread(target=dribble, daemon=True)
            thread.start()
            self.assertEqual(recv_exact(left, 8), b"12345678")
            thread.join(timeout=5)

    def test_recv_exact_raises_when_the_peer_closes_early(self) -> None:
        left, right = socket.socketpair()
        with left:
            right.sendall(b"123")
            right.close()
            with self.assertRaises(ProxyConnectionError):
                recv_exact(left, 8)

    def test_read_until_blank_line_stops_at_the_terminator(self) -> None:
        """
        The tunnelled stream begins on the byte after the blank line.

        Over-reading swallows the start of the server's TLS ServerHello, and
        the failure surfaces later as something resembling a certificate error.
        """
        left, right = socket.socketpair()
        with left, right:
            right.sendall(b"HTTP/1.1 200 OK\r\n\r\nTUNNELBYTES")
            header = read_until_blank_line(left)
            self.assertEqual(header, b"HTTP/1.1 200 OK\r\n\r\n")
            self.assertEqual(recv_exact(left, 11), b"TUNNELBYTES")

    def test_read_until_blank_line_rejects_an_unterminated_response(self) -> None:
        left, right = socket.socketpair()
        with left, right:
            right.sendall(b"x" * 200)
            with self.assertRaises(ProxyConnectionError):
                read_until_blank_line(left, limit=100)

    def test_read_until_blank_line_raises_when_closed_early(self) -> None:
        left, right = socket.socketpair()
        with left:
            right.sendall(b"HTTP/1.1 200 OK\r\n")
            right.close()
            with self.assertRaises(ProxyConnectionError):
                read_until_blank_line(left)


class TestHttpConnectTunnel(unittest.TestCase):
    """The CONNECT exchange against a mock proxy."""

    def test_successful_tunnel_carries_data(self) -> None:
        with MockProxy(http_handler(payload=b"PAYLOAD")) as proxy:
            config = proxy.config(ProxyType.HTTP)
            tunnel = open_http_connect_tunnel(config, "example.com", 443)
            with tunnel:
                self.assertEqual(recv_exact(tunnel, 7), b"PAYLOAD")
        self.assertIsNone(proxy.error)

    def test_request_line_and_host_name_the_destination(self) -> None:
        with MockProxy(http_handler()) as proxy:
            tunnel = open_http_connect_tunnel(
                proxy.config(ProxyType.HTTP), "example.com", 443
            )
            tunnel.close()
        request = proxy.received[0].decode()
        self.assertTrue(request.startswith("CONNECT example.com:443 HTTP/1.1\r\n"))
        self.assertIn("Host: example.com:443\r\n", request)

    def test_credentials_are_sent_preemptively(self) -> None:
        """
        Waiting for a 407 buys nothing when the proxy is known to need them.

        A download opening many connections would pay that round trip on each.
        """
        with MockProxy(http_handler()) as proxy:
            config = proxy.config(ProxyType.HTTP, username="user", password="pass")
            open_http_connect_tunnel(config, "example.com", 443).close()
        request = proxy.received[0].decode()
        # base64("user:pass")
        self.assertIn("Proxy-Authorization: Basic dXNlcjpwYXNz\r\n", request)

    def test_no_authorization_header_without_credentials(self) -> None:
        with MockProxy(http_handler()) as proxy:
            open_http_connect_tunnel(
                proxy.config(ProxyType.HTTP), "example.com", 443
            ).close()
        self.assertNotIn(b"Proxy-Authorization", proxy.received[0])

    def test_ipv6_destination_is_bracketed(self) -> None:
        """Unbracketed, the address colons are indistinguishable from the port."""
        with MockProxy(http_handler()) as proxy:
            open_http_connect_tunnel(
                proxy.config(ProxyType.HTTP), "2001:db8::1", 443
            ).close()
        self.assertIn(b"CONNECT [2001:db8::1]:443 ", proxy.received[0])

    def test_407_raises_authentication_error(self) -> None:
        handler = http_handler(status=b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        with MockProxy(handler) as proxy:
            with self.assertRaises(ProxyAuthenticationError) as caught:
                open_http_connect_tunnel(
                    proxy.config(ProxyType.HTTP), "example.com", 443
                )
        self.assertFalse(caught.exception.is_retryable)

    def test_refusal_raises_connection_error(self) -> None:
        handler = http_handler(status=b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        with MockProxy(handler) as proxy:
            with self.assertRaises(ProxyConnectionError) as caught:
                open_http_connect_tunnel(
                    proxy.config(ProxyType.HTTP), "example.com", 443
                )
        self.assertIn("502", str(caught.exception))
        self.assertTrue(caught.exception.is_retryable)

    def test_malformed_status_line_rejected(self) -> None:
        for status in (b"NOT A STATUS LINE\r\n\r\n", b"HTTP/1.1 notanumber\r\n\r\n"):
            with MockProxy(http_handler(status=status)) as proxy:
                with self.assertRaises(ProxyConnectionError):
                    open_http_connect_tunnel(
                        proxy.config(ProxyType.HTTP), "example.com", 443
                    )

    def test_combined_response_and_payload_does_not_lose_tunnel_bytes(self) -> None:
        """
        The framing bug this module exists to avoid.

        A proxy may legally send the response and the first tunnelled bytes in
        one segment. A buffered reader consumes both and the payload is gone.
        """
        # Shaped like the opening bytes of a TLS ServerHello, which is what
        # would actually be lost here.
        hello = b"\x16\x03\x01FAKE-SERVERHELLO"
        handler = http_handler(payload=hello, combine=True)
        with MockProxy(handler) as proxy:
            tunnel = open_http_connect_tunnel(
                proxy.config(ProxyType.HTTP), "example.com", 443
            )
            with tunnel:
                self.assertEqual(recv_exact(tunnel, len(hello)), hello)

    def test_unreachable_proxy_raises(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()  # nothing is listening now
        config = ProxyConfig(ProxyType.HTTP, "127.0.0.1", port)
        with self.assertRaises(ProxyConnectionError):
            open_http_connect_tunnel(config, "example.com", 443, timeout=2.0)


class TestSocks5Tunnel(unittest.TestCase):
    """The RFC 1928 handshake against a mock proxy."""

    def test_successful_no_auth_tunnel(self) -> None:
        with MockProxy(socks5_handler(payload=b"PAYLOAD")) as proxy:
            tunnel = open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "example.com", 443
            )
            with tunnel:
                self.assertEqual(recv_exact(tunnel, 7), b"PAYLOAD")
        self.assertIsNone(proxy.error)

    def test_greeting_offers_no_auth_when_unconfigured(self) -> None:
        with MockProxy(socks5_handler()) as proxy:
            open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "example.com", 443
            ).close()
        self.assertEqual(proxy.received[0], bytes([0x05, 0x01, 0x00]))

    def test_greeting_offers_userpass_when_configured(self) -> None:
        """
        Offering a method that cannot be completed invites the proxy to select
        it and then fail, which reports as a credential error rather than the
        absent configuration it is.
        """
        with MockProxy(socks5_handler(method=0x02)) as proxy:
            config = proxy.config(ProxyType.SOCKS5H, username="u", password="p")
            open_socks5_tunnel(config, "example.com", 443).close()
        self.assertEqual(proxy.received[0], bytes([0x05, 0x02, 0x02, 0x00]))

    def test_username_password_subnegotiation(self) -> None:
        with MockProxy(socks5_handler(method=0x02)) as proxy:
            config = proxy.config(ProxyType.SOCKS5H, username="alice", password="s3cr3t")
            open_socks5_tunnel(config, "example.com", 443).close()
        self.assertEqual(
            proxy.received[1], bytes([0x01, 5]) + b"alice" + bytes([6]) + b"s3cr3t"
        )

    def test_rejected_credentials_raise_authentication_error(self) -> None:
        with MockProxy(socks5_handler(method=0x02, auth_status=0x01)) as proxy:
            config = proxy.config(ProxyType.SOCKS5H, username="u", password="bad")
            with self.assertRaises(ProxyAuthenticationError) as caught:
                open_socks5_tunnel(config, "example.com", 443)
        self.assertFalse(caught.exception.is_retryable)

    def test_no_acceptable_methods_raises_authentication_error(self) -> None:
        with MockProxy(socks5_handler(method=0xFF)) as proxy:
            with self.assertRaises(ProxyAuthenticationError) as caught:
                open_socks5_tunnel(
                    proxy.config(ProxyType.SOCKS5H), "example.com", 443
                )
        self.assertIn("no credentials were configured", str(caught.exception))

    def test_userpass_selected_without_credentials_raises(self) -> None:
        with MockProxy(socks5_handler(method=0x02)) as proxy:
            with self.assertRaises(ProxyAuthenticationError):
                open_socks5_tunnel(
                    proxy.config(ProxyType.SOCKS5H), "example.com", 443
                )

    def test_unsupported_method_raises_connection_error(self) -> None:
        with MockProxy(socks5_handler(method=0x03)) as proxy:
            with self.assertRaises(ProxyConnectionError):
                open_socks5_tunnel(
                    proxy.config(ProxyType.SOCKS5H), "example.com", 443
                )

    def test_reply_codes_are_reported_in_words(self) -> None:
        for code, text in ((0x02, "not allowed by ruleset"), (0x04, "host unreachable")):
            with MockProxy(socks5_handler(reply=code)) as proxy:
                with self.assertRaises(ProxyConnectionError) as caught:
                    open_socks5_tunnel(
                        proxy.config(ProxyType.SOCKS5H), "example.com", 443
                    )
            self.assertIn(text, str(caught.exception))

    def test_remote_resolution_sends_the_hostname(self) -> None:
        """
        socks5h asks the proxy to resolve, which is what makes an internal
        name reachable and keeps the destination off the local resolver.
        """
        with MockProxy(socks5_handler()) as proxy:
            open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "internal.corp.lan", 8443
            ).close()
        request = proxy.received[1]
        self.assertEqual(request[:4], bytes([0x05, 0x01, 0x00, SOCKS_ATYP_DOMAIN]))
        self.assertEqual(request[4], len("internal.corp.lan"))
        self.assertEqual(request[5:5 + 17], b"internal.corp.lan")
        self.assertEqual(request[-2:], (8443).to_bytes(2, "big"))

    def test_local_resolution_sends_a_literal_address(self) -> None:
        with MockProxy(socks5_handler()) as proxy:
            open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5), "127.0.0.1", 443
            ).close()
        request = proxy.received[1]
        self.assertEqual(request[:4], bytes([0x05, 0x01, 0x00, SOCKS_ATYP_IPV4]))
        self.assertEqual(request[4:8], socket.inet_aton("127.0.0.1"))

    def test_literal_address_is_never_sent_as_a_domain(self) -> None:
        """There is no name for the proxy to resolve, whatever the scheme says."""
        with MockProxy(socks5_handler()) as proxy:
            open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "93.184.216.34", 443
            ).close()
        self.assertEqual(proxy.received[1][3], SOCKS_ATYP_IPV4)

    def test_variable_length_bound_address_is_fully_consumed(self) -> None:
        """
        The bound address is discarded but must still be read.

        Its length varies, and anything left unread arrives as the first bytes
        of the tunnelled stream, corrupting the TLS handshake that follows.
        """
        handler = socks5_handler(bnd_atyp=SOCKS_ATYP_DOMAIN, payload=b"CLEAN")
        with MockProxy(handler) as proxy:
            tunnel = open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "example.com", 443
            )
            with tunnel:
                self.assertEqual(recv_exact(tunnel, 5), b"CLEAN")

    def test_ipv6_bound_address_is_fully_consumed(self) -> None:
        handler = socks5_handler(bnd_atyp=SOCKS_ATYP_IPV6, payload=b"CLEAN")
        with MockProxy(handler) as proxy:
            tunnel = open_socks5_tunnel(
                proxy.config(ProxyType.SOCKS5H), "example.com", 443
            )
            with tunnel:
                self.assertEqual(recv_exact(tunnel, 5), b"CLEAN")

    def test_wrong_protocol_version_rejected(self) -> None:
        def handle(connection: socket.socket, server: MockProxy) -> None:
            recv_exact(connection, 2)
            connection.sendall(bytes([0x04, 0x00]))

        with MockProxy(handle) as proxy:
            with self.assertRaises(ProxyConnectionError) as caught:
                open_socks5_tunnel(
                    proxy.config(ProxyType.SOCKS5H), "example.com", 443
                )
        self.assertIn("SOCKS version 4", str(caught.exception))


class TestAddressEncoding(unittest.TestCase):
    """RFC 1928 address encoding, independent of any connection."""

    def test_domain_is_length_prefixed(self) -> None:
        encoded = encode_socks5_address("example.com", 443, resolve_remotely=True)
        self.assertEqual(encoded[0], SOCKS_ATYP_DOMAIN)
        self.assertEqual(encoded[1], 11)
        self.assertEqual(encoded[2:13], b"example.com")
        self.assertEqual(encoded[13:], (443).to_bytes(2, "big"))

    def test_ipv4(self) -> None:
        encoded = encode_socks5_address("93.184.216.34", 80, resolve_remotely=True)
        self.assertEqual(encoded, bytes([SOCKS_ATYP_IPV4]) + socket.inet_aton("93.184.216.34") + b"\x00\x50")

    def test_ipv6(self) -> None:
        encoded = encode_socks5_address("2001:db8::1", 443, resolve_remotely=True)
        self.assertEqual(encoded[0], SOCKS_ATYP_IPV6)
        self.assertEqual(len(encoded), 19)

    def test_overlong_hostname_rejected(self) -> None:
        """A domain is length-prefixed with one byte and cannot exceed 255."""
        with self.assertRaises(ProxyError) as caught:
            encode_socks5_address("a" * 256 + ".com", 443, resolve_remotely=True)
        self.assertIn("255", str(caught.exception))

    def test_local_resolution_failure_suggests_socks5h(self) -> None:
        """
        The common cause is an internal name that only resolves inside.

        The error says so rather than reporting a bare DNS failure.
        """
        with self.assertRaises(ProxyError) as caught:
            encode_socks5_address(
                "no-such-host.invalid", 443, resolve_remotely=False
            )
        self.assertIn("socks5h", str(caught.exception))


class TestSslContext(unittest.TestCase):
    """Corporate CA bundles and the verification switch."""

    def test_default_context_verifies(self) -> None:
        context = build_ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_custom_bundle_is_loaded(self) -> None:
        """
        A TLS-inspecting proxy re-signs with an authority the system does not
        trust, so the bundle is the difference between working and failing.
        """
        with TemporaryDirectory() as directory:
            bundle = Path(directory) / "corp-ca.pem"
            bundle.write_text(ssl.get_default_verify_paths().cafile and
                              Path(ssl.get_default_verify_paths().cafile).read_text()
                              or "")
            if not bundle.read_text().strip():
                self.skipTest("no system CA bundle available to copy")
            context = build_ssl_context(ca_bundle=str(bundle))
            self.assertGreater(len(context.get_ca_certs()), 0)

    def test_unreadable_bundle_raises(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaises(ProxyError):
                build_ssl_context(ca_bundle=str(Path(directory) / "absent.pem"))

    def test_verification_can_be_disabled(self) -> None:
        context = build_ssl_context(verify=False)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


class TestProxyTunnel(unittest.TestCase):
    """The high-level entry point and its dispatch."""

    def test_dispatches_to_http_connect(self) -> None:
        with MockProxy(http_handler(payload=b"HTTPOK")) as proxy:
            tunnel = ProxyTunnel(proxy.config(ProxyType.HTTP))
            connection = tunnel.open("example.com", 443)
            with connection:
                self.assertEqual(recv_exact(connection, 6), b"HTTPOK")

    def test_dispatches_to_socks5(self) -> None:
        with MockProxy(socks5_handler(payload=b"SOCKSOK")) as proxy:
            tunnel = ProxyTunnel(proxy.config(ProxyType.SOCKS5H))
            connection = tunnel.open("example.com", 443)
            with connection:
                self.assertEqual(recv_exact(connection, 7), b"SOCKSOK")

    def test_invalid_construction_rejected(self) -> None:
        config = ProxyConfig(ProxyType.HTTP, "p", 8080)
        with self.assertRaises(ProxyError):
            ProxyTunnel("not-a-config")  # type: ignore[arg-type]
        for bad in (0, -1):
            with self.assertRaises(ProxyError):
                ProxyTunnel(config, timeout=bad)
        with self.assertRaises(ProxyError):
            ProxyTunnel(config, timeout="30")  # type: ignore[arg-type]

    def test_invalid_target_rejected(self) -> None:
        tunnel = ProxyTunnel(ProxyConfig(ProxyType.HTTP, "p", 8080))
        with self.assertRaises(ProxyError):
            tunnel.open("", 443)
        for port in (0, 65536):
            with self.assertRaises(ProxyError):
                tunnel.open("example.com", port)
        with self.assertRaises(ProxyError):
            tunnel.open("example.com", "443")  # type: ignore[arg-type]

    def test_tls_failure_through_the_tunnel_is_reported_as_proxy_error(self) -> None:
        """
        The mock is not a TLS server, so the handshake must fail cleanly rather
        than leaking a raw SSLError from inside the tunnel.
        """
        with MockProxy(http_handler(payload=b"not-tls")) as proxy:
            tunnel = ProxyTunnel(proxy.config(ProxyType.HTTP))
            with self.assertRaises(ProxyError):
                tunnel.open_tls("example.com", 443)

    def test_tls_verifies_against_the_destination_not_the_proxy(self) -> None:
        """
        The security-critical property of tunnelled TLS.

        SNI and certificate verification must target the destination. Verifying
        against the proxy's name would accept any certificate the proxy could
        obtain for itself, which is exactly the interception verification exists
        to detect — and it would still appear to work, so nothing else catches
        it.
        """
        observed = {}
        real_build = proxy_module.build_ssl_context

        def spying_build(ca_bundle=None, verify=True):
            context = real_build(ca_bundle, verify)
            real_wrap = context.wrap_socket

            def wrap(sock, server_hostname=None, **kwargs):
                observed["server_hostname"] = server_hostname
                raise ssl.SSLError("stop before handshaking")

            context.wrap_socket = wrap  # type: ignore[method-assign]
            return context

        with MockProxy(http_handler()) as proxy:
            config = proxy.config(ProxyType.HTTP)
            tunnel = ProxyTunnel(config)
            proxy_module.build_ssl_context = spying_build
            try:
                with self.assertRaises(ProxyError):
                    tunnel.open_tls("secure.example.com", 443)
            finally:
                proxy_module.build_ssl_context = real_build

        self.assertEqual(observed["server_hostname"], "secure.example.com")
        self.assertNotEqual(observed["server_hostname"], config.host)

    def test_defaults_and_repr(self) -> None:
        tunnel = ProxyTunnel(ProxyConfig(ProxyType.HTTP, "proxy.corp", 8080))
        self.assertEqual(tunnel.timeout, DEFAULT_TIMEOUT)
        self.assertIsNone(tunnel.ca_bundle)
        self.assertIn("proxy.corp", repr(tunnel))


if __name__ == "__main__":
    unittest.main()
