"""
Unit tests for the AWS S3 SigV4 range adapter in src.adapters.s3_adapter.
Anchored on AWS's official published test vectors for both the signing key
derivation and a Range-bearing GET Object request, then covering
canonicalization rules, the credential resolution chain, URI and endpoint
resolution including the DNS-compatibility fallback, and presigned pass-through.
"""

from __future__ import annotations

import configparser
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.adapters.base import SignedRequest, format_range_header
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
from src.exceptions import ConfigurationError

# AWS publishes two worked examples that use secrets differing by a single
# character. Mixing them up produces a correct implementation that fails its
# own test, so they are named apart deliberately.
SIGNING_KEY_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
S3_EXAMPLE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
S3_EXAMPLE_KEY_ID = "AKIAIOSFODNN7EXAMPLE"

EXAMPLE_CREDENTIALS = AWSCredentials(S3_EXAMPLE_KEY_ID, S3_EXAMPLE_SECRET)
EXAMPLE_TIME = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)


def fixed_clock(moment: datetime = EXAMPLE_TIME):
    """A clock frozen at a known instant, so signatures are reproducible."""
    return lambda: moment


def adapter(**kwargs) -> S3Adapter:
    """Build an adapter with example credentials and a frozen clock."""
    kwargs.setdefault("credentials", EXAMPLE_CREDENTIALS)
    kwargs.setdefault("region", "us-east-1")
    kwargs.setdefault("clock", fixed_clock())
    kwargs.setdefault("environ", {})
    return S3Adapter(**kwargs)


class TestOfficialAWSVectors(unittest.TestCase):
    """
    Acceptance criterion: signatures match AWS's official test vectors.

    Two independent vectors, covering both halves of the scheme. Matching a
    256-bit value by accident is not possible, so a pass here is strong evidence
    the whole canonicalization and derivation chain is right.
    """

    def test_signing_key_derivation_vector(self) -> None:
        """AWS's documented "derive a signing key" example (iam, 20120215)."""
        signer = SigV4Signer(
            AWSCredentials("AKIDEXAMPLE", SIGNING_KEY_SECRET),
            region="us-east-1",
            service="iam",
        )
        self.assertEqual(
            signer.signing_key("20120215").hex(),
            "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d",
        )

    def test_get_object_with_range_vector(self) -> None:
        """
        AWS's documented "GET Object" example, which carries a Range header.

        Exactly this project's use case: a signed partial read.
        """
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")
        headers = signer.sign(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            headers={
                "Host": "examplebucket.s3.amazonaws.com",
                "Range": "bytes=0-9",
            },
            timestamp=EXAMPLE_TIME,
        )
        self.assertEqual(
            headers["Authorization"],
            f"{ALGORITHM} Credential={S3_EXAMPLE_KEY_ID}/20130524/us-east-1/s3/"
            "aws4_request, SignedHeaders=host;range;x-amz-content-sha256;"
            "x-amz-date, Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170"
            "aba48dd91039c6036bdb41",
        )

    def test_canonical_request_matches_the_published_document(self) -> None:
        """
        The intermediate values, checked too.

        A signature mismatch is otherwise undiagnosable — one hex string with no
        indication which of the six canonical components differed.
        """
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")
        canonical = signer.build_canonical_request(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            headers={
                "Host": "examplebucket.s3.amazonaws.com",
                "Range": "bytes=0-9",
                "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256,
                "x-amz-date": "20130524T000000Z",
            },
            payload_hash=EMPTY_PAYLOAD_SHA256,
            timestamp=EXAMPLE_TIME,
        )
        self.assertEqual(
            canonical.canonical_request,
            "GET\n"
            "/test.txt\n"
            "\n"
            "host:examplebucket.s3.amazonaws.com\n"
            "range:bytes=0-9\n"
            f"x-amz-content-sha256:{EMPTY_PAYLOAD_SHA256}\n"
            "x-amz-date:20130524T000000Z\n"
            "\n"
            "host;range;x-amz-content-sha256;x-amz-date\n"
            f"{EMPTY_PAYLOAD_SHA256}",
        )
        self.assertEqual(
            canonical.string_to_sign,
            f"{ALGORITHM}\n"
            "20130524T000000Z\n"
            "20130524/us-east-1/s3/aws4_request\n"
            "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972",
        )
        self.assertEqual(canonical.credential_scope, "20130524/us-east-1/s3/aws4_request")

    def test_the_two_aws_example_secrets_are_genuinely_different(self) -> None:
        """
        Guards the trap that cost real debugging time here.

        The signing-key example and the S3 example differ by one character, and
        swapping them yields a correct implementation that fails its own vector.
        """
        self.assertNotEqual(SIGNING_KEY_SECRET, S3_EXAMPLE_SECRET)
        self.assertEqual(
            sum(a != b for a, b in zip(SIGNING_KEY_SECRET, S3_EXAMPLE_SECRET)), 1
        )


class TestRangeIsSigned(unittest.TestCase):
    """
    The signature must cover the Range header, not merely accompany it.

    Otherwise an intermediary could rewrite which bytes are served, and a
    transfer verifying chunks against a manifest would fail integrity checks
    for no visible reason.
    """

    def test_range_appears_in_signed_headers(self) -> None:
        request = adapter().build_range_request("s3://examplebucket/test.txt", 0, 9)
        self.assertIn("SignedHeaders=", request.header("Authorization"))
        signed = request.header("Authorization").split("SignedHeaders=")[1]
        self.assertIn("range", signed.split(",")[0])

    def test_changing_the_range_changes_the_signature(self) -> None:
        pool = adapter()
        first = pool.build_range_request("s3://examplebucket/test.txt", 0, 9)
        second = pool.build_range_request("s3://examplebucket/test.txt", 10, 19)
        self.assertNotEqual(
            first.header("Authorization"), second.header("Authorization")
        )

    def test_identical_requests_sign_identically(self) -> None:
        """Signing is a pure function of the request and the clock."""
        pool = adapter()
        first = pool.build_range_request("s3://examplebucket/test.txt", 0, 9)
        second = pool.build_range_request("s3://examplebucket/test.txt", 0, 9)
        self.assertEqual(first.headers, second.headers)

    def test_payload_hash_is_the_empty_digest_for_a_get(self) -> None:
        request = adapter().build_range_request("s3://examplebucket/k", 0, 9)
        self.assertEqual(request.header("x-amz-content-sha256"), EMPTY_PAYLOAD_SHA256)


class TestCanonicalization(unittest.TestCase):
    """The rules that decide whether a signature will verify."""

    def setUp(self) -> None:
        self.signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")

    def test_headers_are_lowercased_trimmed_and_sorted(self) -> None:
        block, signed = self.signer.canonical_headers(
            {"Zeta": " last ", "Host": "example.com", "Alpha": "a    b"}
        )
        self.assertEqual(signed, "alpha;host;zeta")
        self.assertEqual(block, "alpha:a b\nhost:example.com\nzeta:last\n")

    def test_sequential_whitespace_is_collapsed(self) -> None:
        """
        Intermediaries may legally reformat whitespace.

        A signature that depended on it would break in transit.
        """
        block, _ = self.signer.canonical_headers({"x": "a  \t  b"})
        self.assertEqual(block, "x:a b\n")

    def test_query_string_is_sorted_and_encoded(self) -> None:
        self.assertEqual(
            self.signer.canonical_query_string("b=2&a=1&a=0"), "a=0&a=1&b=2"
        )

    def test_empty_query_string(self) -> None:
        self.assertEqual(self.signer.canonical_query_string(""), "")

    def test_query_values_are_uri_encoded(self) -> None:
        self.assertEqual(
            self.signer.canonical_query_string("k=a/b c"), "k=a%2Fb%20c"
        )

    def test_empty_path_canonicalizes_to_root(self) -> None:
        self.assertEqual(self.signer.canonical_uri(""), "/")

    def test_s3_encodes_the_path_once(self) -> None:
        """
        Every AWS service double-encodes the path except S3.

        Getting it wrong yields valid-looking signatures S3 rejects for any key
        containing an encodable character.
        """
        self.assertFalse(self.signer.encodes_path_twice)
        self.assertEqual(self.signer.canonical_uri("/a b/c"), "/a%20b/c")

    def test_other_services_encode_the_path_twice(self) -> None:
        other = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="execute-api")
        self.assertTrue(other.encodes_path_twice)
        self.assertEqual(other.canonical_uri("/a b/c"), "/a%2520b/c")

    def test_unreserved_characters_are_never_encoded(self) -> None:
        self.assertEqual(
            self.signer.canonical_uri("/a-b_c.d~e"), "/a-b_c.d~e"
        )

    def test_slashes_survive_in_paths(self) -> None:
        self.assertEqual(self.signer.canonical_uri("/a/b/c"), "/a/b/c")


class TestSessionTokenAndTimestamps(unittest.TestCase):
    """Temporary credentials and clock handling."""

    def test_session_token_is_signed_not_merely_sent(self) -> None:
        """
        An unsigned token could be stripped or swapped without invalidating the
        signature, so it must appear in SignedHeaders.
        """
        signer = SigV4Signer(
            AWSCredentials("AK", "secret", session_token="TOKEN"),
            region="us-east-1",
            service="s3",
        )
        headers = signer.sign(
            "GET", "https://b.s3.amazonaws.com/k", {"Host": "b.s3.amazonaws.com"},
            timestamp=EXAMPLE_TIME,
        )
        self.assertEqual(headers["x-amz-security-token"], "TOKEN")
        self.assertIn("x-amz-security-token", headers["Authorization"])

    def test_no_token_header_without_a_session(self) -> None:
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")
        headers = signer.sign(
            "GET", "https://b.s3.amazonaws.com/k", {"Host": "b.s3.amazonaws.com"},
            timestamp=EXAMPLE_TIME,
        )
        self.assertNotIn("x-amz-security-token", headers)

    def test_naive_timestamp_rejected(self) -> None:
        """
        A naive datetime would be signed as UTC whatever the host's zone.

        The resulting signature is rejected by AWS as clock skew, which is a
        confusing way to learn about a missing tzinfo.
        """
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")
        with self.assertRaises(ConfigurationError):
            signer.sign(
                "GET", "https://b.s3.amazonaws.com/k", {"Host": "b"},
                timestamp=datetime(2013, 5, 24),
            )

    def test_non_utc_timestamp_is_converted(self) -> None:
        """An aware timestamp in another zone is correct, just not in UTC yet."""
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, region="us-east-1", service="s3")
        elsewhere = EXAMPLE_TIME.astimezone(timezone(timedelta(hours=5, minutes=30)))
        headers = signer.sign(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
            {"Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9"},
            timestamp=elsewhere,
        )
        self.assertIn(
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
            headers["Authorization"],
        )

    def test_signer_rejects_bad_configuration(self) -> None:
        with self.assertRaises(ConfigurationError):
            SigV4Signer("not-credentials")  # type: ignore[arg-type]
        for bad in ("", None):
            with self.assertRaises(ConfigurationError):
                SigV4Signer(EXAMPLE_CREDENTIALS, region=bad)  # type: ignore[arg-type]
            with self.assertRaises(ConfigurationError):
                SigV4Signer(EXAMPLE_CREDENTIALS, service=bad)  # type: ignore[arg-type]


class TestCredentials(unittest.TestCase):
    """Credential resolution in AWS's documented precedence order."""

    def test_secret_is_redacted_in_the_repr(self) -> None:
        """The secret must never reach a log or a traceback."""
        text = repr(AWSCredentials("AKIA123", "super-secret-value"))
        self.assertNotIn("super-secret-value", text)
        self.assertIn("AKIA123", text)
        self.assertIn("redacted", text)

    def test_invalid_credentials_rejected(self) -> None:
        for key, secret in (("", "s"), ("a", ""), (None, "s"), ("a", None)):
            with self.assertRaises(ConfigurationError):
                AWSCredentials(key, secret)  # type: ignore[arg-type]

    def test_temporary_flag(self) -> None:
        self.assertFalse(AWSCredentials("a", "b").is_temporary)
        self.assertTrue(AWSCredentials("a", "b", session_token="t").is_temporary)

    def test_environment_provider(self) -> None:
        creds = credentials_from_environment(
            {
                "AWS_ACCESS_KEY_ID": "AK",
                "AWS_SECRET_ACCESS_KEY": "SK",
                "AWS_SESSION_TOKEN": "ST",
            }
        )
        self.assertEqual(creds.access_key_id, "AK")
        self.assertEqual(creds.session_token, "ST")

    def test_environment_provider_returns_none_when_unset(self) -> None:
        """Absence must not be an error; the chain has more to try."""
        self.assertIsNone(credentials_from_environment({}))
        self.assertIsNone(credentials_from_environment({"AWS_ACCESS_KEY_ID": "AK"}))

    def test_shared_file_provider(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            path.write_text(
                "[default]\n"
                "aws_access_key_id = AKFILE\n"
                "aws_secret_access_key = SKFILE\n"
                "\n[other]\n"
                "aws_access_key_id = AKOTHER\n"
                "aws_secret_access_key = SKOTHER\n"
                "aws_session_token = STOTHER\n"
            )
            default = credentials_from_shared_file(path=path)
            self.assertEqual(default.access_key_id, "AKFILE")
            other = credentials_from_shared_file(profile="other", path=path)
            self.assertEqual(other.session_token, "STOTHER")
            self.assertIsNone(credentials_from_shared_file(profile="absent", path=path))

    def test_missing_shared_file_returns_none(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsNone(
                credentials_from_shared_file(path=Path(directory) / "nope")
            )

    def test_malformed_shared_file_raises(self) -> None:
        """
        A credentials file that exists but cannot be parsed is a
        misconfiguration to report, not a reason to fall through to anonymous.
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            path.write_text("this is not ini\n[[[\n")
            with self.assertRaises(ConfigurationError):
                credentials_from_shared_file(path=path)

    def test_instance_metadata_provider(self) -> None:
        creds = credentials_from_instance_metadata(
            lambda: {
                "AccessKeyId": "AKIMDS",
                "SecretAccessKey": "SKIMDS",
                "Token": "TOKENIMDS",
            }
        )
        self.assertEqual(creds.access_key_id, "AKIMDS")
        self.assertTrue(creds.is_temporary)

    def test_instance_metadata_absent(self) -> None:
        self.assertIsNone(credentials_from_instance_metadata(lambda: {}))
        self.assertIsNone(
            credentials_from_instance_metadata(lambda: {"AccessKeyId": "only"})
        )

    def test_precedence_order(self) -> None:
        """Explicit, then environment, then file, then metadata."""
        explicit = AWSCredentials("EXPLICIT", "s")
        environ = {"AWS_ACCESS_KEY_ID": "ENV", "AWS_SECRET_ACCESS_KEY": "s"}
        metadata = lambda: {"AccessKeyId": "IMDS", "SecretAccessKey": "s"}  # noqa: E731

        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials"
            path.write_text(
                "[default]\naws_access_key_id = FILE\naws_secret_access_key = s\n"
            )
            self.assertEqual(
                resolve_credentials(
                    explicit=explicit, environ=environ, shared_file=path,
                    metadata_fetcher=metadata,
                ).access_key_id,
                "EXPLICIT",
            )
            self.assertEqual(
                resolve_credentials(
                    environ=environ, shared_file=path, metadata_fetcher=metadata
                ).access_key_id,
                "ENV",
            )
            self.assertEqual(
                resolve_credentials(
                    environ={}, shared_file=path, metadata_fetcher=metadata
                ).access_key_id,
                "FILE",
            )
            self.assertEqual(
                resolve_credentials(
                    environ={},
                    shared_file=Path(directory) / "absent",
                    metadata_fetcher=metadata,
                ).access_key_id,
                "IMDS",
            )

    def test_no_credentials_anywhere_raises(self) -> None:
        """
        Anonymous access is not a fallback.

        An unsigned request to a private bucket fails with a 403 that reads as a
        permissions problem rather than the missing-configuration problem it is.
        """
        with TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError) as caught:
                resolve_credentials(
                    environ={}, shared_file=Path(directory) / "absent"
                )
            self.assertIn("No AWS credentials found", str(caught.exception))


class TestUriParsing(unittest.TestCase):
    """A manifest may carry any of the three S3 reference forms."""

    def setUp(self) -> None:
        self.pool = adapter()

    def test_s3_scheme(self) -> None:
        location = self.pool.parse_uri("s3://my-bucket/path/to/object.tar.gz")
        self.assertEqual(location.container, "my-bucket")
        self.assertEqual(location.key, "path/to/object.tar.gz")

    def test_virtual_hosted_https_url(self) -> None:
        location = self.pool.parse_uri(
            "https://my-bucket.s3.us-west-2.amazonaws.com/path/f.iso"
        )
        self.assertEqual(location.container, "my-bucket")
        self.assertEqual(location.key, "path/f.iso")
        self.assertEqual(location.region, "us-west-2")

    def test_path_style_https_url(self) -> None:
        location = self.pool.parse_uri(
            "https://s3.eu-west-1.amazonaws.com/my-bucket/path/f.iso"
        )
        self.assertEqual(location.container, "my-bucket")
        self.assertEqual(location.key, "path/f.iso")
        self.assertEqual(location.region, "eu-west-1")

    def test_legacy_global_endpoint_names_no_region(self) -> None:
        """
        ``bucket.s3.amazonaws.com`` implies us-east-1 but does not say so.

        Returning None lets the adapter's configured region apply instead of a
        guess baked into the URL.
        """
        location = self.pool.parse_uri("https://examplebucket.s3.amazonaws.com/test.txt")
        self.assertEqual(location.container, "examplebucket")
        self.assertIsNone(location.region)

    def test_missing_key_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("s3://bucket-only")

    def test_missing_bucket_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("s3:///key-only")

    def test_non_s3_host_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.parse_uri("https://example.com/bucket/key")

    def test_unsupported_scheme_rejected(self) -> None:
        for uri in ("ftp://bucket/key", "gs://bucket/key"):
            with self.assertRaises(ConfigurationError):
                self.pool.parse_uri(uri)

    def test_empty_uri_rejected(self) -> None:
        for bad in ("", None, 5):
            with self.assertRaises(ConfigurationError):
                self.pool.parse_uri(bad)  # type: ignore[arg-type]

    def test_handles_reports_scheme_ownership(self) -> None:
        self.assertTrue(self.pool.handles("s3://b/k"))
        self.assertFalse(self.pool.handles("gs://b/k"))
        self.assertFalse(self.pool.handles(None))  # type: ignore[arg-type]


class TestEndpointResolution(unittest.TestCase):
    """Virtual-hosted addressing, and the cases that force path-style."""

    def test_virtual_hosted_by_default(self) -> None:
        pool = adapter(region="eu-central-1")
        host, path = pool.endpoint_for(pool.parse_uri("s3://my-bucket/key.iso"))
        self.assertEqual(host, "my-bucket.s3.eu-central-1.amazonaws.com")
        self.assertEqual(path, "/key.iso")

    def test_dotted_bucket_falls_back_to_path_style(self) -> None:
        """
        A dotted name is legal but breaks the wildcard certificate.

        ``my.bucket.s3.amazonaws.com`` has two labels where ``*.s3...`` covers
        one, so the request fails TLS verification rather than returning an S3
        error the caller could interpret.
        """
        pool = adapter()
        host, path = pool.endpoint_for(pool.parse_uri("s3://my.dotted.bucket/key"))
        self.assertEqual(host, "s3.us-east-1.amazonaws.com")
        self.assertEqual(path, "/my.dotted.bucket/key")

    def test_force_path_style(self) -> None:
        pool = adapter(force_path_style=True)
        host, path = pool.endpoint_for(pool.parse_uri("s3://my-bucket/key"))
        self.assertEqual(host, "s3.us-east-1.amazonaws.com")
        self.assertEqual(path, "/my-bucket/key")

    def test_transfer_acceleration_endpoint(self) -> None:
        pool = adapter(accelerate=True)
        host, _ = pool.endpoint_for(pool.parse_uri("s3://my-bucket/key"))
        self.assertEqual(host, "my-bucket.s3-accelerate.amazonaws.com")

    def test_custom_endpoint(self) -> None:
        """S3-compatible stores are addressed the same way."""
        pool = adapter(endpoint="storage.example.net")
        host, path = pool.endpoint_for(pool.parse_uri("s3://my-bucket/key"))
        self.assertEqual(host, "my-bucket.storage.example.net")
        self.assertEqual(path, "/key")

    def test_custom_endpoint_with_path_style(self) -> None:
        pool = adapter(endpoint="storage.example.net", force_path_style=True)
        host, path = pool.endpoint_for(pool.parse_uri("s3://my-bucket/key"))
        self.assertEqual(host, "storage.example.net")
        self.assertEqual(path, "/my-bucket/key")

    def test_url_derived_endpoint_is_preserved(self) -> None:
        """An explicit URL already names its host; do not second-guess it."""
        pool = adapter(region="us-east-1")
        location = pool.parse_uri("https://b.s3.ap-south-1.amazonaws.com/k")
        host, path = pool.endpoint_for(location)
        self.assertEqual(host, "b.s3.ap-south-1.amazonaws.com")
        self.assertEqual(path, "/k")

    def test_accelerate_conflicts_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            adapter(accelerate=True, endpoint="storage.example.net")
        with self.assertRaises(ConfigurationError):
            adapter(accelerate=True, force_path_style=True)

    def test_region_comes_from_the_environment_when_unset(self) -> None:
        pool = S3Adapter(
            credentials=EXAMPLE_CREDENTIALS,
            environ={"AWS_REGION": "ap-northeast-1"},
            clock=fixed_clock(),
        )
        self.assertEqual(pool.region, "ap-northeast-1")

    def test_region_defaults_when_nothing_is_configured(self) -> None:
        pool = S3Adapter(
            credentials=EXAMPLE_CREDENTIALS, environ={}, clock=fixed_clock()
        )
        self.assertEqual(pool.region, DEFAULT_REGION)


class TestBucketNameRules(unittest.TestCase):
    """DNS compatibility decides whether virtual-hosted addressing is safe."""

    def test_acceptable_names(self) -> None:
        for name in ("my-bucket", "abc", "a" * 63, "bucket123"):
            self.assertTrue(is_dns_compatible_bucket(name), name)

    def test_unacceptable_names(self) -> None:
        for name in (
            "ab",               # too short
            "a" * 64,           # too long
            "my.bucket",        # breaks the wildcard certificate
            "my_bucket",        # underscore is not DNS-legal
            "MyBucket",         # uppercase
            "-leading",
            "trailing-",
            "has space",
        ):
            self.assertFalse(is_dns_compatible_bucket(name), name)


class TestRangeRequestConstruction(unittest.TestCase):
    """The complete artifact handed to the transport."""

    def setUp(self) -> None:
        self.pool = adapter()

    def test_request_shape(self) -> None:
        request = self.pool.build_range_request("s3://examplebucket/test.txt", 0, 9)
        self.assertIsInstance(request, SignedRequest)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url, "https://examplebucket.s3.us-east-1.amazonaws.com/test.txt")
        self.assertEqual(request.range_header, "bytes=0-9")
        self.assertEqual(request.size, 10)

    def test_host_header_matches_the_url(self) -> None:
        """A signed Host that disagreed with the URL would fail verification."""
        request = self.pool.build_range_request("s3://examplebucket/test.txt", 0, 9)
        self.assertEqual(request.header("Host"), "examplebucket.s3.us-east-1.amazonaws.com")
        self.assertIn(request.header("Host"), request.url)

    def test_key_with_spaces_is_encoded_in_the_url(self) -> None:
        request = self.pool.build_range_request("s3://examplebucket/a b/c.iso", 0, 9)
        self.assertIn("/a%20b/c.iso", request.url)

    def test_header_lookup_is_case_insensitive(self) -> None:
        """Providers are inconsistent about capitalization."""
        request = self.pool.build_range_request("s3://examplebucket/k", 0, 9)
        self.assertEqual(request.header("range"), request.header("Range"))
        self.assertEqual(request.header("AUTHORIZATION"), request.header("Authorization"))
        self.assertIsNone(request.header("X-Absent"))

    def test_request_is_immutable(self) -> None:
        request = self.pool.build_range_request("s3://examplebucket/k", 0, 9)
        with self.assertRaises(Exception):
            request.url = "https://evil.example"  # type: ignore[misc]

    def test_headers_are_copied_not_aliased(self) -> None:
        """
        The headers are the signed artifact.

        A caller holding a reference to the mapping it passed in must not be
        able to edit them after signing.
        """
        mutable = {"Range": "bytes=0-9"}
        request = SignedRequest("GET", "https://x/y", mutable, 0, 9)
        mutable["Range"] = "bytes=0-999999"
        self.assertEqual(request.header("Range"), "bytes=0-9")

    def test_invalid_ranges_rejected(self) -> None:
        for start, end in ((-1, 10), (10, 5), (0, -1)):
            with self.assertRaises(ConfigurationError):
                self.pool.build_range_request("s3://examplebucket/k", start, end)
        for bad in (1.5, "0", True, None):
            with self.assertRaises(ConfigurationError):
                format_range_header(bad, 10)  # type: ignore[arg-type]

    def test_single_byte_range(self) -> None:
        request = self.pool.build_range_request("s3://examplebucket/k", 5, 5)
        self.assertEqual(request.range_header, "bytes=5-5")
        self.assertEqual(request.size, 1)


class TestPresignedPassthrough(unittest.TestCase):
    """
    A presigned URL carries someone else's credential and must not be re-signed.

    S3 does not cover Range in a query-string signature, so adding the header is
    legitimate; overwriting the Authorization would replace the caller's
    credential with ours and fail.
    """

    def setUp(self) -> None:
        self.pool = adapter()
        self.presigned = (
            "https://examplebucket.s3.amazonaws.com/test.txt"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400"
            "&X-Amz-SignedHeaders=host&X-Amz-Signature=abcdef"
        )

    def test_detection(self) -> None:
        self.assertTrue(S3Adapter.is_presigned(self.presigned))
        self.assertFalse(S3Adapter.is_presigned("https://examplebucket.s3.amazonaws.com/t"))
        self.assertFalse(S3Adapter.is_presigned("s3://bucket/key"))

    def test_url_is_passed_through_untouched(self) -> None:
        request = self.pool.build_range_request(self.presigned, 0, 9)
        self.assertEqual(request.url, self.presigned)

    def test_range_is_added_but_nothing_is_signed(self) -> None:
        request = self.pool.build_range_request(self.presigned, 0, 9)
        self.assertEqual(request.header("Range"), "bytes=0-9")
        self.assertIsNone(request.header("Authorization"))
        self.assertIsNone(request.header("x-amz-date"))


class TestAdapterRepr(unittest.TestCase):
    """The repr appears in transfer logs and must not leak the secret."""

    def test_repr_omits_the_secret(self) -> None:
        text = repr(adapter())
        self.assertIn("us-east-1", text)
        self.assertIn(S3_EXAMPLE_KEY_ID, text)
        self.assertNotIn(S3_EXAMPLE_SECRET, text)


if __name__ == "__main__":
    unittest.main()
