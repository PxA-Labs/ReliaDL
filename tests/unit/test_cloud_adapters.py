"""
Unit tests for the GCS and Azure Blob range adapters in
src.adapters.gcs_adapter and src.adapters.azure_adapter. Covers the signed JWT
assertion GCS service accounts exchange for a token, application default
credential discovery, Azure Shared Key signing against Microsoft's documented
string-to-sign example, the x-ms-range versus Range distinction that decides
which positional slot a range is signed in, and the SAS and bearer paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from src.adapters.azure_adapter import (
    AZURE_API_VERSION,
    AZURE_BLOB_SUFFIX,
    AzureAuthMode,
    AzureBlobAdapter,
    AzureSharedKeyCredential,
    AzureSharedKeySigner,
    format_rfc1123,
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
from src.exceptions import ConfigurationError

ACCOUNT_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
FIXED_TIME = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)


def rsa_pem() -> str:
    """Generate a throwaway RSA private key in PEM form."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def service_account_document(**overrides) -> dict:
    """A well-formed service account key document."""
    document = {
        "type": "service_account",
        "project_id": "my-project",
        "private_key_id": "key-abc123",
        "private_key": rsa_pem(),
        "client_email": "svc@my-project.iam.gserviceaccount.com",
        "token_uri": GCS_TOKEN_URI,
    }
    document.update(overrides)
    return document


def decode_segment(segment: str) -> dict:
    """Decode a base64url JWT segment, restoring the stripped padding."""
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class TestServiceAccountLoading(unittest.TestCase):
    """Key documents must be validated before anything tries to sign with them."""

    def test_from_dict(self) -> None:
        account = GCSServiceAccount.from_dict(service_account_document())
        self.assertEqual(account.client_email, "svc@my-project.iam.gserviceaccount.com")
        self.assertEqual(account.private_key_id, "key-abc123")
        self.assertEqual(account.project_id, "my-project")

    def test_authorized_user_document_rejected(self) -> None:
        """
        Authorized-user credentials carry a refresh token, not a signing key.

        They cannot produce an assertion, and saying so beats failing later
        inside the PEM parser.
        """
        with self.assertRaises(ConfigurationError) as caught:
            GCSServiceAccount.from_dict(
                {"type": "authorized_user", "client_id": "x", "refresh_token": "y"}
            )
        self.assertIn("service_account", str(caught.exception))

    def test_missing_required_fields_rejected(self) -> None:
        for field in ("client_email", "private_key"):
            document = service_account_document()
            del document[field]
            with self.assertRaises(ConfigurationError):
                GCSServiceAccount.from_dict(document)

    def test_from_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sa.json"
            path.write_text(json.dumps(service_account_document()))
            account = GCSServiceAccount.from_file(path)
            self.assertEqual(account.project_id, "my-project")

    def test_missing_file_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                GCSServiceAccount.from_file(Path(directory) / "absent.json")

    def test_malformed_json_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sa.json"
            path.write_text("{not json")
            with self.assertRaises(ConfigurationError):
                GCSServiceAccount.from_file(path)

    def test_non_object_json_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sa.json"
            path.write_text("[1, 2, 3]")
            with self.assertRaises(ConfigurationError):
                GCSServiceAccount.from_file(path)

    def test_unparseable_private_key_rejected(self) -> None:
        account = GCSServiceAccount("svc@x.com", "-----BEGIN PRIVATE KEY-----\nnope\n")
        with self.assertRaises(ConfigurationError):
            account.load_private_key()

    def test_non_rsa_key_rejected(self) -> None:
        """Google signs assertions with RS256; an Ed25519 key cannot."""
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        with self.assertRaises(ConfigurationError) as caught:
            GCSServiceAccount("svc@x.com", pem).load_private_key()
        self.assertIn("RSA", str(caught.exception))

    def test_private_key_is_redacted_in_the_repr(self) -> None:
        account = GCSServiceAccount.from_dict(service_account_document())
        text = repr(account)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertIn("redacted", text)

    def test_invalid_fields_rejected(self) -> None:
        for email, key in (("", "pem"), ("svc@x", ""), (None, "pem")):
            with self.assertRaises(ConfigurationError):
                GCSServiceAccount(email, key)  # type: ignore[arg-type]


class TestAssertionSigning(unittest.TestCase):
    """The JWT a service account exchanges for an access token."""

    def setUp(self) -> None:
        self.document = service_account_document()
        self.account = GCSServiceAccount.from_dict(self.document)

    def test_structure(self) -> None:
        assertion = self.account.build_assertion(issued_at=1700000000)
        segments = assertion.split(".")
        self.assertEqual(len(segments), 3)

        header = decode_segment(segments[0])
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "JWT")
        self.assertEqual(header["kid"], "key-abc123")

        claims = decode_segment(segments[1])
        self.assertEqual(claims["iss"], self.account.client_email)
        self.assertEqual(claims["aud"], GCS_TOKEN_URI)
        self.assertEqual(claims["scope"], GCS_READ_SCOPE)
        self.assertEqual(claims["iat"], 1700000000)
        self.assertEqual(claims["exp"], 1700003600)

    def test_base64url_carries_no_padding(self) -> None:
        """
        JWT is defined over base64url without '='.

        A padded assertion is rejected with an invalid_grant error that says
        nothing about the encoding being the problem.
        """
        assertion = self.account.build_assertion(issued_at=1700000000)
        self.assertNotIn("=", assertion)
        self.assertNotIn("+", assertion)
        self.assertNotIn("/", assertion)

    def test_signature_verifies_against_the_public_key(self) -> None:
        """Real verification, not merely a well-shaped string."""
        assertion = self.account.build_assertion(issued_at=1700000000)
        header, claims, signature = assertion.split(".")
        signing_input = f"{header}.{claims}".encode("ascii")
        raw_signature = base64.urlsafe_b64decode(
            signature + "=" * (-len(signature) % 4)
        )
        public_key = self.account.load_private_key().public_key()
        # Raises InvalidSignature if the assertion was not signed by this key.
        public_key.verify(
            raw_signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
        )

    def test_a_tampered_assertion_fails_verification(self) -> None:
        from cryptography.exceptions import InvalidSignature

        assertion = self.account.build_assertion(issued_at=1700000000)
        header, claims, signature = assertion.split(".")
        forged = base64.urlsafe_b64encode(
            json.dumps(
                {**decode_segment(claims), "scope": "https://www.googleapis.com/auth/cloud-platform"},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).rstrip(b"=").decode()

        raw_signature = base64.urlsafe_b64decode(
            signature + "=" * (-len(signature) % 4)
        )
        public_key = self.account.load_private_key().public_key()
        with self.assertRaises(InvalidSignature):
            public_key.verify(
                raw_signature,
                f"{header}.{forged}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

    def test_scope_is_read_only_by_default(self) -> None:
        """
        A downloader has no reason to hold a token that can write.

        Narrowing the scope bounds what a leaked token can damage.
        """
        self.assertIn("read_only", GCS_READ_SCOPE)
        claims = decode_segment(
            self.account.build_assertion(issued_at=1).split(".")[1]
        )
        self.assertEqual(claims["scope"], GCS_READ_SCOPE)

    def test_key_id_is_omitted_when_absent(self) -> None:
        account = GCSServiceAccount.from_dict(
            service_account_document(private_key_id=None)
        )
        header = decode_segment(account.build_assertion(issued_at=1).split(".")[0])
        self.assertNotIn("kid", header)

    def test_lifetime_validation(self) -> None:
        """Google rejects an over-long assertion rather than clamping it."""
        for bad in (0, -1, 3601, 86400):
            with self.assertRaises(ConfigurationError):
                self.account.build_assertion(lifetime_seconds=bad)
        for bad_type in (60.0, "3600", True):
            with self.assertRaises(ConfigurationError):
                self.account.build_assertion(lifetime_seconds=bad_type)  # type: ignore[arg-type]

    def test_assertion_is_reproducible_for_a_given_instant(self) -> None:
        first = self.account.build_assertion(issued_at=1700000000)
        second = self.account.build_assertion(issued_at=1700000000)
        self.assertEqual(first, second)


class TestApplicationDefaultCredentials(unittest.TestCase):
    """Discovery of a credentials file, in Google's documented order."""

    def test_environment_variable_wins(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sa.json"
            path.write_text("{}")
            found = find_application_default_credentials(
                environ={CREDENTIALS_ENV: str(path)}
            )
            self.assertEqual(found, path)

    def test_environment_variable_pointing_nowhere_raises(self) -> None:
        """
        A path that is set but wrong is a misconfiguration, not an absence.

        Falling through silently would produce an unauthenticated request and a
        403 that hides the typo.
        """
        with TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                find_application_default_credentials(
                    environ={CREDENTIALS_ENV: str(Path(directory) / "absent.json")}
                )

    def test_well_known_fallback(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "adc.json"
            path.write_text("{}")
            self.assertEqual(
                find_application_default_credentials(environ={}, well_known=path), path
            )

    def test_nothing_found_returns_none(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsNone(
                find_application_default_credentials(
                    environ={}, well_known=Path(directory) / "absent.json"
                )
            )


class TestGCSAdapter(unittest.TestCase):
    """URI resolution and bearer authorization for GCS reads."""

    def setUp(self) -> None:
        self.pool = GCSAdapter(access_token="ya29.TOKEN")

    def test_parse_gs_scheme(self) -> None:
        location = self.pool.parse_uri("gs://my-bucket/path/to/file.img")
        self.assertEqual(location.container, "my-bucket")
        self.assertEqual(location.key, "path/to/file.img")

    def test_parse_https_url(self) -> None:
        location = self.pool.parse_uri(
            f"https://{GCS_HOST}/my-bucket/path/to/file.img"
        )
        self.assertEqual(location.container, "my-bucket")
        self.assertEqual(location.key, "path/to/file.img")

    def test_non_gcs_host_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("https://example.com/bucket/key")

    def test_unsupported_scheme_rejected(self) -> None:
        for uri in ("s3://bucket/key", "ftp://bucket/key", ""):
            with self.assertRaises(ConfigurationError):
                self.pool.parse_uri(uri)

    def test_missing_key_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("gs://bucket-only")

    def test_request_shape(self) -> None:
        request = self.pool.build_range_request("gs://my-bucket/f.img", 0, 1023)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url, f"https://{GCS_HOST}/my-bucket/f.img")
        self.assertEqual(request.header("Range"), "bytes=0-1023")
        self.assertEqual(request.header("Authorization"), "Bearer ya29.TOKEN")
        self.assertEqual(request.header("Host"), GCS_HOST)

    def test_object_names_keep_their_slashes(self) -> None:
        """
        GCS buckets are flat; a slash is an ordinary character in the name.

        Encoding it would address a different object than the one named.
        """
        request = self.pool.build_range_request("gs://b/a/b/c.img", 0, 9)
        self.assertTrue(request.url.endswith("/b/a/b/c.img"))

    def test_object_names_are_otherwise_encoded(self) -> None:
        request = self.pool.build_range_request("gs://b/a file.img", 0, 9)
        self.assertIn("a%20file.img", request.url)

    def test_token_provider_is_consulted_per_request(self) -> None:
        """
        Access tokens expire inside an hour; large transfers do not.

        Resolving once would strand a transfer partway through with a 401 that
        reads as a permissions change.
        """
        tokens = iter(["first", "second", "third"])
        pool = GCSAdapter(token_provider=lambda: next(tokens))
        seen = [
            pool.build_range_request("gs://b/k", 0, 9).header("Authorization")
            for _ in range(3)
        ]
        self.assertEqual(
            seen, ["Bearer first", "Bearer second", "Bearer third"]
        )

    def test_missing_token_source_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            GCSAdapter().build_range_request("gs://b/k", 0, 9)

    def test_empty_token_from_provider_rejected(self) -> None:
        pool = GCSAdapter(token_provider=lambda: "")
        with self.assertRaises(ConfigurationError):
            pool.build_range_request("gs://b/k", 0, 9)

    def test_conflicting_token_sources_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            GCSAdapter(access_token="a", token_provider=lambda: "b")

    def test_empty_access_token_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            GCSAdapter(access_token="")

    def test_handles_scheme(self) -> None:
        self.assertTrue(self.pool.handles("gs://b/k"))
        self.assertFalse(self.pool.handles("s3://b/k"))

    def test_repr_does_not_leak_the_token(self) -> None:
        self.assertNotIn("ya29.TOKEN", repr(self.pool))


class TestRfc1123(unittest.TestCase):
    """Azure signs an RFC 1123 date and is strict about its shape."""

    def test_format(self) -> None:
        self.assertEqual(format_rfc1123(FIXED_TIME), "Fri, 24 May 2013 00:00:00 GMT")

    def test_names_are_english_regardless_of_locale(self) -> None:
        """
        strftime's %a and %b follow the process locale.

        A host running under a non-English locale would produce a date Azure
        cannot parse, so the names are written out instead.
        """
        rendered = format_rfc1123(datetime(2024, 3, 4, 5, 6, 7, tzinfo=timezone.utc))
        self.assertEqual(rendered, "Mon, 04 Mar 2024 05:06:07 GMT")

    def test_converted_to_gmt(self) -> None:
        elsewhere = FIXED_TIME.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(format_rfc1123(elsewhere), "Fri, 24 May 2013 00:00:00 GMT")

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            format_rfc1123(datetime(2013, 5, 24))


class TestAzureSharedKeySigning(unittest.TestCase):
    """
    The string to sign, anchored on Microsoft's own documented example.

    A Shared Key failure returns only a 403, so the intermediate document is the
    only thing that makes a mismatch diagnosable.
    """

    def setUp(self) -> None:
        self.credential = AzureSharedKeyCredential("contosorest", ACCOUNT_KEY)
        self.signer = AzureSharedKeySigner(self.credential)

    def test_microsoft_documented_string_to_sign(self) -> None:
        """Reproduces the List Containers example from the Azure REST docs."""
        document = self.signer.string_to_sign(
            method="GET",
            account="contosorest",
            path="/",
            headers={
                "x-ms-date": "Fri, 17 Nov 2017 01:07:37 GMT",
                "x-ms-version": "2017-07-29",
            },
            query="comp=list",
        )
        self.assertEqual(
            document,
            "GET\n\n\n\n\n\n\n\n\n\n\n\n"
            "x-ms-date:Fri, 17 Nov 2017 01:07:37 GMT\n"
            "x-ms-version:2017-07-29\n"
            "/contosorest/\ncomp:list",
        )

    def test_positional_slots_are_empty_lines_not_absent_lines(self) -> None:
        """Every field is positional, so omitting one shifts all the rest."""
        document = self.signer.string_to_sign(
            "GET", "acct", "/c/b", {"x-ms-date": "D"}
        )
        self.assertEqual(document.split("\n")[0], "GET")
        self.assertEqual(document.split("\n")[1:12], [""] * 11)

    def test_content_length_is_empty_not_zero(self) -> None:
        """A rule that changed in API version 2015-02-21 and still trips code."""
        document = self.signer.string_to_sign(
            "GET", "acct", "/c/b", {"Content-Length": "0", "x-ms-date": "D"}
        )
        self.assertEqual(document.split("\n")[3], "")

    def test_date_slot_is_empty_when_x_ms_date_is_present(self) -> None:
        """
        Exactly one date is authoritative.

        Signing both invites a mismatch when an intermediary rewrites Date.
        """
        document = self.signer.string_to_sign(
            "GET", "acct", "/c/b", {"Date": "ignored", "x-ms-date": "D"}
        )
        self.assertEqual(document.split("\n")[6], "")

    def test_only_x_ms_headers_are_canonicalized(self) -> None:
        block = self.signer.canonicalized_headers(
            {"x-ms-b": "2", "x-ms-a": "1", "Host": "h", "Authorization": "a"}
        )
        self.assertEqual(block, "x-ms-a:1\nx-ms-b:2\n")

    def test_canonicalized_header_values_are_trimmed(self) -> None:
        self.assertEqual(
            self.signer.canonicalized_headers({"x-ms-a": "  a   b  "}), "x-ms-a:a b\n"
        )

    def test_canonicalized_resource_without_query(self) -> None:
        self.assertEqual(
            self.signer.canonicalized_resource("acct", "/c/b"), "/acct/c/b"
        )

    def test_canonicalized_resource_sorts_query_parameters(self) -> None:
        self.assertEqual(
            self.signer.canonicalized_resource("acct", "/c/b", "z=1&a=2"),
            "/acct/c/b\na:2\nz:1",
        )

    def test_repeated_query_values_are_comma_joined_in_order(self) -> None:
        self.assertEqual(
            self.signer.canonicalized_resource("acct", "/c/b", "k=b&k=a"),
            "/acct/c/b\nk:a,b",
        )

    def test_signature_matches_an_independent_hmac(self) -> None:
        """Checks the crypto step against a computation that shares no code."""
        document = self.signer.string_to_sign(
            "GET", "contosorest", "/c/b", {"x-ms-date": "D"}
        )
        expected = base64.b64encode(
            hmac.new(
                base64.b64decode(ACCOUNT_KEY), document.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode()
        self.assertEqual(self.signer.signature(document), expected)

    def test_authorization_header_shape(self) -> None:
        header = self.signer.authorization("GET", "/c/b", {"x-ms-date": "D"})
        self.assertTrue(header.startswith("SharedKey contosorest:"))

    def test_credential_validation(self) -> None:
        with self.assertRaises(ConfigurationError):
            AzureSharedKeyCredential("", ACCOUNT_KEY)
        with self.assertRaises(ConfigurationError):
            AzureSharedKeyCredential("acct", "")

    def test_non_base64_key_rejected_at_construction(self) -> None:
        """
        A mistyped key becomes a configuration error, not a 403 on chunk one.
        """
        with self.assertRaises(ConfigurationError):
            AzureSharedKeyCredential("acct", "not-base64!!!")

    def test_key_is_redacted_in_the_repr(self) -> None:
        text = repr(AzureSharedKeyCredential("acct", ACCOUNT_KEY))
        self.assertNotIn(ACCOUNT_KEY, text)
        self.assertIn("redacted", text)

    def test_signer_rejects_a_non_credential(self) -> None:
        with self.assertRaises(ConfigurationError):
            AzureSharedKeySigner("account-key")  # type: ignore[arg-type]


class TestAzureRangeHeaderChoice(unittest.TestCase):
    """
    Which header the range travels in decides which slot it is signed in.

    This is the detail most easily got wrong: a range sent as x-ms-range is
    covered by the canonicalized header block and leaves the positional Range
    slot empty, while a standard Range header populates that slot instead.
    """

    def setUp(self) -> None:
        self.credential = AzureSharedKeyCredential("myaccount", ACCOUNT_KEY)

    def _adapter(self, **kwargs) -> AzureBlobAdapter:
        kwargs.setdefault("shared_key", self.credential)
        kwargs.setdefault("clock", lambda: FIXED_TIME)
        return AzureBlobAdapter(**kwargs)

    def test_x_ms_range_is_the_default(self) -> None:
        request = self._adapter().build_range_request("az://c/b.iso", 0, 1023)
        self.assertEqual(request.header("x-ms-range"), "bytes=0-1023")
        self.assertIsNone(request.header("Range"))

    def test_x_ms_range_leaves_the_positional_slot_empty(self) -> None:
        request = self._adapter().build_range_request("az://c/b.iso", 0, 1023)
        document = AzureSharedKeySigner(self.credential).string_to_sign(
            "GET", "myaccount", "/c/b.iso", dict(request.headers)
        )
        self.assertEqual(document.split("\n")[11], "")
        self.assertIn("x-ms-range:bytes=0-1023", document)

    def test_standard_range_populates_the_positional_slot(self) -> None:
        request = self._adapter(use_x_ms_range=False).build_range_request(
            "az://c/b.iso", 0, 1023
        )
        self.assertEqual(request.header("Range"), "bytes=0-1023")
        self.assertIsNone(request.header("x-ms-range"))
        document = AzureSharedKeySigner(self.credential).string_to_sign(
            "GET", "myaccount", "/c/b.iso", dict(request.headers)
        )
        self.assertEqual(document.split("\n")[11], "bytes=0-1023")
        self.assertNotIn("x-ms-range", document)

    def test_the_two_modes_sign_differently(self) -> None:
        """A signature valid for one spelling is invalid for the other."""
        with_x_ms = self._adapter().build_range_request("az://c/b.iso", 0, 1023)
        with_range = self._adapter(use_x_ms_range=False).build_range_request(
            "az://c/b.iso", 0, 1023
        )
        self.assertNotEqual(
            with_x_ms.header("Authorization"), with_range.header("Authorization")
        )

    def test_changing_the_range_changes_the_signature(self) -> None:
        pool = self._adapter()
        first = pool.build_range_request("az://c/b.iso", 0, 1023)
        second = pool.build_range_request("az://c/b.iso", 1024, 2047)
        self.assertNotEqual(
            first.header("Authorization"), second.header("Authorization")
        )


class TestAzureAdapter(unittest.TestCase):
    """URI resolution and the three authorization modes."""

    def setUp(self) -> None:
        self.credential = AzureSharedKeyCredential("myaccount", ACCOUNT_KEY)
        self.pool = AzureBlobAdapter(
            shared_key=self.credential, clock=lambda: FIXED_TIME
        )

    def test_parse_az_scheme(self) -> None:
        location = self.pool.parse_uri("az://mycontainer/path/blob.iso")
        self.assertEqual(location.container, "mycontainer")
        self.assertEqual(location.key, "path/blob.iso")

    def test_parse_https_url_carries_the_account(self) -> None:
        location = self.pool.parse_uri(
            f"https://other.{AZURE_BLOB_SUFFIX}/c/b.iso"
        )
        self.assertEqual(location.region, "other")
        self.assertEqual(location.container, "c")
        self.assertEqual(location.key, "b.iso")

    def test_https_account_overrides_the_configured_one(self) -> None:
        request = self.pool.build_range_request(
            f"https://other.{AZURE_BLOB_SUFFIX}/c/b.iso", 0, 9
        )
        self.assertEqual(request.header("Host"), f"other.{AZURE_BLOB_SUFFIX}")

    def test_az_scheme_without_a_configured_account_rejected(self) -> None:
        pool = AzureBlobAdapter(sas_token="sv=2023-11-03&sig=abc")
        with self.assertRaises(ConfigurationError):
            pool.parse_uri("az://c/b.iso")

    def test_non_azure_host_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("https://example.com/c/b")

    def test_unsupported_scheme_rejected(self) -> None:
        for uri in ("gs://c/b", "ftp://c/b", ""):
            with self.assertRaises(ConfigurationError):
                self.pool.parse_uri(uri)

    def test_request_shape(self) -> None:
        request = self.pool.build_range_request("az://mycontainer/b.iso", 0, 1023)
        self.assertEqual(
            request.url,
            f"https://myaccount.{AZURE_BLOB_SUFFIX}/mycontainer/b.iso",
        )
        self.assertEqual(request.header("x-ms-version"), AZURE_API_VERSION)
        self.assertEqual(request.header("x-ms-date"), "Fri, 24 May 2013 00:00:00 GMT")
        self.assertTrue(request.header("Authorization").startswith("SharedKey myaccount:"))

    def test_api_version_is_pinned(self) -> None:
        """
        Several signed fields have changed meaning between versions.

        A floating version would silently alter what a correct signature is.
        """
        self.assertEqual(self.pool.api_version, AZURE_API_VERSION)
        pinned = AzureBlobAdapter(
            shared_key=self.credential, api_version="2020-10-02",
            clock=lambda: FIXED_TIME,
        )
        request = pinned.build_range_request("az://c/b", 0, 9)
        self.assertEqual(request.header("x-ms-version"), "2020-10-02")

    def test_sas_mode_appends_the_token_and_signs_nothing(self) -> None:
        """A SAS is itself the authorization; a header would conflict."""
        pool = AzureBlobAdapter(
            account_name="myaccount",
            sas_token="?sv=2023-11-03&sig=abc%3D",
            clock=lambda: FIXED_TIME,
        )
        self.assertIs(pool.auth_mode, AzureAuthMode.SAS)
        request = pool.build_range_request("az://c/b.iso", 0, 9)
        self.assertIn("sv=2023-11-03&sig=abc%3D", request.url)
        self.assertIsNone(request.header("Authorization"))
        self.assertEqual(request.header("x-ms-range"), "bytes=0-9")

    def test_sas_leading_question_mark_is_optional(self) -> None:
        with_mark = AzureBlobAdapter(
            account_name="a", sas_token="?sv=1", clock=lambda: FIXED_TIME
        ).build_range_request("az://c/b", 0, 9)
        without = AzureBlobAdapter(
            account_name="a", sas_token="sv=1", clock=lambda: FIXED_TIME
        ).build_range_request("az://c/b", 0, 9)
        self.assertEqual(with_mark.url, without.url)

    def test_bearer_mode(self) -> None:
        pool = AzureBlobAdapter(
            account_name="myaccount", access_token="eyJ0token",
            clock=lambda: FIXED_TIME,
        )
        self.assertIs(pool.auth_mode, AzureAuthMode.BEARER)
        request = pool.build_range_request("az://c/b.iso", 0, 9)
        self.assertEqual(request.header("Authorization"), "Bearer eyJ0token")

    def test_bearer_provider_is_consulted_per_request(self) -> None:
        tokens = iter(["one", "two"])
        pool = AzureBlobAdapter(
            account_name="a", token_provider=lambda: next(tokens),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(
            [
                pool.build_range_request("az://c/b", 0, 9).header("Authorization")
                for _ in range(2)
            ],
            ["Bearer one", "Bearer two"],
        )

    def test_empty_bearer_token_rejected(self) -> None:
        pool = AzureBlobAdapter(
            account_name="a", token_provider=lambda: "", clock=lambda: FIXED_TIME
        )
        with self.assertRaises(ConfigurationError):
            pool.build_range_request("az://c/b", 0, 9)

    def test_multiple_credentials_rejected(self) -> None:
        """
        Choosing by precedence would fail with a 403 naming neither credential.

        Refusing up front is far easier to diagnose.
        """
        with self.assertRaises(ConfigurationError):
            AzureBlobAdapter(shared_key=self.credential, sas_token="sv=1")
        with self.assertRaises(ConfigurationError):
            AzureBlobAdapter(shared_key=self.credential, access_token="t")
        with self.assertRaises(ConfigurationError):
            AzureBlobAdapter(sas_token="sv=1", access_token="t")

    def test_no_credential_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            AzureBlobAdapter(account_name="a")

    def test_blob_names_keep_their_slashes(self) -> None:
        request = self.pool.build_range_request("az://c/a/b/c.iso", 0, 9)
        self.assertTrue(request.url.endswith("/c/a/b/c.iso"))

    def test_handles_scheme(self) -> None:
        self.assertTrue(self.pool.handles("az://c/b"))
        self.assertFalse(self.pool.handles("gs://c/b"))

    def test_invalid_ranges_rejected(self) -> None:
        for start, end in ((-1, 10), (10, 5)):
            with self.assertRaises(ConfigurationError):
                self.pool.build_range_request("az://c/b", start, end)

    def test_repr_does_not_leak_the_key(self) -> None:
        text = repr(self.pool)
        self.assertNotIn(ACCOUNT_KEY, text)
        self.assertIn("SHARED_KEY", text)


if __name__ == "__main__":
    unittest.main()
