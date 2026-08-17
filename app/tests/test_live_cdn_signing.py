"""Phase 6b (docs/AI_HANDOFF.md Sec 8): focused unit tests for
app/live_cdn_signing.py's sign_segment_url()/cryptography_rsa_signer()/
get_configured_signer().

Deliberately self-contained: no `main`/`partner_db` import (so no
test-discovery-order concern applies -- see test_live_view_page_
characterization.py's docstring for the pre-existing issue this avoids),
no real network/AWS call anywhere. botocore.signers.CloudFrontSigner's own
policy-building logic is exercised directly (not mocked) since it does no
I/O itself -- only the rsa_signer callback is ever faked/injected.

get_configured_signer()'s AWS calls (STS assume-role, Secrets Manager
GetSecretValue) are exercised against a fake `boto3` module swapped in via
patch.object(live_cdn_signing, "boto3", ...) -- never the real boto3, never
a real network call. Because get_configured_signer() lazily caches its
result at module level (live_cdn_signing._cached_signer), every test class
that calls it resets that module global to None in setUp/tearDown so no
test can observe a signer cached by a different test.
"""

import inspect
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botocore.signers import CloudFrontSigner

import live_cdn_signing
from live_cdn_signing import (
    SIGNED_SEGMENT_URL_TTL_SECONDS,
    cryptography_rsa_signer,
    get_configured_signer,
    sign_segment_url,
)

FIXED_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

VALID_KWARGS = dict(
    cloudfront_base_url="https://d31cxfv0l904ar.cloudfront.net",
    customer_id="cust-1",
    site_id="site-1",
    appliance_id="app-1",
    camera_id="cam-1",
    segment_filename="camera1_42.ts",
    key_id="KEYPAIRID123",
    now=FIXED_NOW,
)

# A fully-populated, valid set of get_configured_signer() env vars. Values
# are shaped like the real AWS identifiers but are never used against real
# AWS -- boto3 itself is always swapped for a fake in these tests.
FULL_SIGNING_ENV = {
    "ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN": (
        "arn:aws:iam::880690594006:role/anyaicam-cloudfront-signing-key-reader"
    ),
    "ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME": "anyaicam/live-relay/cloudfront-private-key",
    "ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_REGION": "us-east-1",
}

FAKE_STS_CREDENTIALS = {
    "Credentials": {
        "AccessKeyId": "FAKE-ACCESS-KEY-ID",
        "SecretAccessKey": "fake-secret-access-key",
        "SessionToken": "fake-session-token",
    }
}


def _fake_boto3(sts_result=None, sts_error=None, secret_result=None, secret_error=None):
    """Builds a fake boto3 module stand-in whose .client("sts"|"secretsmanager")
    returns a MagicMock configured to either return the given result or
    raise the given error -- never touches real boto3 or the network."""

    def client(service_name, **_kwargs):
        if service_name == "sts":
            mock_client = MagicMock()
            if sts_error is not None:
                mock_client.assume_role.side_effect = sts_error
            else:
                mock_client.assume_role.return_value = sts_result
            return mock_client
        if service_name == "secretsmanager":
            mock_client = MagicMock()
            if secret_error is not None:
                mock_client.get_secret_value.side_effect = secret_error
            else:
                mock_client.get_secret_value.return_value = secret_result
            return mock_client
        raise AssertionError(f"unexpected boto3.client service_name={service_name!r}")

    fake_module = MagicMock()
    fake_module.client.side_effect = client
    return fake_module


def _generate_signing_key_pem() -> tuple[object, str]:
    """Returns (private_key_object, pem_text) for a throwaway RSA key --
    standing in for "the key Secrets Manager would have returned", never a
    real production key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return private_key, pem


class RecordingSigner:
    """Fake rsa_signer that records the exact message bytes it was asked to
    sign and returns a fixed, obviously-fake signature -- never real crypto,
    used to prove *what* would be signed without needing a real key."""

    def __init__(self):
        self.messages = []

    def __call__(self, message: bytes) -> bytes:
        self.messages.append(message)
        return b"fake-signature"


class SignSegmentUrlStructureTests(unittest.TestCase):
    def test_valid_signing_returns_a_url_with_expected_structure(self):
        signer = RecordingSigner()
        url = sign_segment_url(**{**VALID_KWARGS, "rsa_signer": signer})
        expected_path = "https://d31cxfv0l904ar.cloudfront.net/live/cust-1/site-1/app-1/cam-1/camera1_42.ts"
        self.assertTrue(url.startswith(expected_path + "?Policy="))
        self.assertIn("&Key-Pair-Id=KEYPAIRID123", url)

    def test_signed_policy_resource_and_expiry_match_expected(self):
        signer = RecordingSigner()
        sign_segment_url(**{**VALID_KWARGS, "rsa_signer": signer})
        self.assertEqual(len(signer.messages), 1)
        policy = json.loads(signer.messages[0].decode("utf8"))
        statement = policy["Statement"][0]
        self.assertEqual(
            statement["Resource"],
            "https://d31cxfv0l904ar.cloudfront.net/live/cust-1/site-1/app-1/cam-1/camera1_42.ts",
        )
        expected_epoch = int((FIXED_NOW + timedelta(seconds=SIGNED_SEGMENT_URL_TTL_SECONDS)).timestamp())
        self.assertEqual(statement["Condition"]["DateLessThan"]["AWS:EpochTime"], expected_epoch)

    def test_expiry_uses_the_fixed_ttl_constant(self):
        self.assertEqual(SIGNED_SEGMENT_URL_TTL_SECONDS, 20)

    def test_no_expires_at_parameter_exists(self):
        params = inspect.signature(sign_segment_url).parameters
        self.assertNotIn("expires_at", params)
        self.assertIn("now", params)


class SignSegmentUrlValidationTests(unittest.TestCase):
    def test_rejects_empty_required_components(self):
        for field in ("cloudfront_base_url", "customer_id", "site_id", "appliance_id", "camera_id", "key_id"):
            with self.subTest(field=field):
                kwargs = {**VALID_KWARGS, "rsa_signer": RecordingSigner(), field: ""}
                with self.assertRaises(ValueError):
                    sign_segment_url(**kwargs)

    def test_rejects_non_callable_rsa_signer(self):
        with self.assertRaises(ValueError):
            sign_segment_url(**{**VALID_KWARGS, "rsa_signer": "not-callable"})

    def test_rejects_empty_segment_filename(self):
        with self.assertRaises(ValueError):
            sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner(), "segment_filename": ""})

    def test_rejects_path_separators_in_filename(self):
        for bad in ("camera1/../etc.ts", "camera1\\x.ts", "../camera1_1.ts", "camera1_1.ts/../x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner(), "segment_filename": bad})

    def test_rejects_wrong_extension(self):
        with self.assertRaises(ValueError):
            sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner(), "segment_filename": "camera1_1.mp4"})

    def test_rejects_missing_camera_number_prefix(self):
        for bad in ("segment_1.ts", "cam1_1.ts", "1camera1_1.ts", "CAMERA1_1.ts"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner(), "segment_filename": bad})

    def test_accepts_valid_filename_shapes(self):
        for good in ("camera1_0.ts", "camera12_999.ts", "camera1.ts"):
            with self.subTest(good=good):
                url = sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner(), "segment_filename": good})
                self.assertIn(f"/{good}?Policy=", url)


class SignSegmentUrlPrefixEscapeTests(unittest.TestCase):
    def test_component_with_embedded_slash_does_not_escape_the_prefix(self):
        signer = RecordingSigner()
        url = sign_segment_url(**{**VALID_KWARGS, "rsa_signer": signer, "camera_id": "abc/../../etc"})
        path_portion = url.split("?", 1)[0].removeprefix("https://d31cxfv0l904ar.cloudfront.net/")
        segments = path_portion.split("/")
        # Exactly 6 segments -- live, customer, site, appliance, (encoded)
        # camera_id, filename -- proving the embedded "/" characters in
        # camera_id were percent-encoded, not interpreted as extra path
        # separators that could escape the intended prefix.
        self.assertEqual(len(segments), 6)
        self.assertEqual(segments[0], "live")
        self.assertNotIn("..", segments)
        self.assertIn("%2F", path_portion)

    def test_signed_resource_reflects_the_encoded_not_raw_component(self):
        signer = RecordingSigner()
        sign_segment_url(**{**VALID_KWARGS, "rsa_signer": signer, "camera_id": "abc/def"})
        policy = json.loads(signer.messages[0].decode("utf8"))
        resource = policy["Statement"][0]["Resource"]
        self.assertNotIn("/abc/def/", resource)
        self.assertIn("abc%2Fdef", resource)


class SignSegmentUrlFailClosedTests(unittest.TestCase):
    def setUp(self):
        # get_configured_signer()'s module-level cache must never leak
        # between tests -- see test_get_configured_signer_returns_none below.
        live_cdn_signing._cached_signer = None

    def tearDown(self):
        live_cdn_signing._cached_signer = None

    def test_cloudfrontsigner_unavailable_raises_runtime_error(self):
        with patch.object(live_cdn_signing, "CloudFrontSigner", None):
            with self.assertRaises(RuntimeError):
                sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner()})

    def test_drift_guard_trips_if_live_relay_s3_prefix_diverges(self):
        with patch.object(live_cdn_signing, "live_relay_s3_prefix", return_value="something/else/entirely/"):
            with self.assertRaises(RuntimeError):
                sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner()})

    def test_get_configured_signer_returns_none_when_unconfigured(self):
        # No ANYAICAM_CLOUDFRONT_SIGNING_KEY_* env vars are set in the test
        # environment, so this must fail closed with no AWS call attempted.
        for name in FULL_SIGNING_ENV:
            self.assertNotIn(name, os.environ)
        self.assertIsNone(get_configured_signer())


class GetConfiguredSignerConfigTests(unittest.TestCase):
    """Covers the missing-env-var and boto3-unavailable fail-closed paths.
    Every test resets the module-level signer cache in setUp/tearDown so a
    signer cached by an earlier or later test can never leak into this
    class's assertions."""

    def setUp(self):
        live_cdn_signing._cached_signer = None
        for name in FULL_SIGNING_ENV:
            os.environ.pop(name, None)

    def tearDown(self):
        live_cdn_signing._cached_signer = None
        for name in FULL_SIGNING_ENV:
            os.environ.pop(name, None)

    def test_returns_none_when_any_required_env_var_is_missing(self):
        for missing in FULL_SIGNING_ENV:
            with self.subTest(missing=missing):
                live_cdn_signing._cached_signer = None
                partial_env = {k: v for k, v in FULL_SIGNING_ENV.items() if k != missing}
                with patch.dict(os.environ, partial_env, clear=False):
                    self.assertIsNone(get_configured_signer())

    def test_returns_none_when_boto3_unavailable(self):
        with patch.dict(os.environ, FULL_SIGNING_ENV, clear=False), patch.object(live_cdn_signing, "boto3", None):
            self.assertIsNone(get_configured_signer())


class GetConfiguredSignerFailureTests(unittest.TestCase):
    """STS failure, Secrets Manager failure, empty/malformed key -- all
    must return None, never raise, and never be cached. Cache is reset in
    setUp/tearDown for the same reason as GetConfiguredSignerConfigTests."""

    def setUp(self):
        live_cdn_signing._cached_signer = None
        self._env_patch = patch.dict(os.environ, FULL_SIGNING_ENV, clear=False)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        live_cdn_signing._cached_signer = None

    def test_sts_assume_role_failure_returns_none_and_is_not_cached(self):
        fake_boto3 = _fake_boto3(sts_error=RuntimeError("assume_role boom"))
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            with self.assertLogs("anyaicam.live_cdn_signing", level="ERROR"):
                self.assertIsNone(get_configured_signer())
            self.assertIsNone(live_cdn_signing._cached_signer)
            # A second call retries from scratch rather than short-circuiting
            # on a cached failure -- proven by boto3.client being invoked
            # again (2 total "sts" attempts across the two calls).
            self.assertIsNone(get_configured_signer())
        self.assertEqual(fake_boto3.client.call_count, 2)

    def test_secrets_manager_get_secret_value_failure_returns_none(self):
        fake_boto3 = _fake_boto3(
            sts_result=FAKE_STS_CREDENTIALS,
            secret_error=RuntimeError("get_secret_value boom"),
        )
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            with self.assertLogs("anyaicam.live_cdn_signing", level="ERROR"):
                self.assertIsNone(get_configured_signer())

    def test_missing_secret_string_key_returns_none(self):
        fake_boto3 = _fake_boto3(sts_result=FAKE_STS_CREDENTIALS, secret_result={"ARN": "arn:aws:secretsmanager:..."})
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            self.assertIsNone(get_configured_signer())

    def test_empty_secret_string_returns_none(self):
        fake_boto3 = _fake_boto3(sts_result=FAKE_STS_CREDENTIALS, secret_result={"SecretString": "   "})
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            self.assertIsNone(get_configured_signer())

    def test_malformed_pem_returns_none(self):
        fake_boto3 = _fake_boto3(
            sts_result=FAKE_STS_CREDENTIALS,
            secret_result={"SecretString": "not-a-real-pem-key"},
        )
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            with self.assertLogs("anyaicam.live_cdn_signing", level="ERROR"):
                self.assertIsNone(get_configured_signer())

    def test_log_output_never_contains_secret_or_credential_values(self):
        """Negative-content check: even on a logged failure, the private
        key PEM and the temporary AWS credentials must never appear in any
        log record -- only the static event-name message is allowed."""
        canary_pem = "-----BEGIN CANARY-SECRET-PEM-VALUE-----not-real-key-material"
        canary_access_key = "CANARY-FAKE-ACCESS-KEY-ID"
        canary_secret_key = "CANARY-FAKE-SECRET-ACCESS-KEY"
        canary_session_token = "CANARY-FAKE-SESSION-TOKEN"
        fake_boto3 = _fake_boto3(
            sts_result={
                "Credentials": {
                    "AccessKeyId": canary_access_key,
                    "SecretAccessKey": canary_secret_key,
                    "SessionToken": canary_session_token,
                }
            },
            secret_result={"SecretString": canary_pem},
        )
        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            with self.assertLogs("anyaicam.live_cdn_signing", level="ERROR") as captured:
                self.assertIsNone(get_configured_signer())
        log_text = "\n".join(captured.output)
        self.assertNotIn(canary_pem, log_text)
        self.assertNotIn(canary_access_key, log_text)
        self.assertNotIn(canary_secret_key, log_text)
        self.assertNotIn(canary_session_token, log_text)


class GetConfiguredSignerSuccessTests(unittest.TestCase):
    """Success path + caching. Cache reset in setUp/tearDown for the same
    reason as the other get_configured_signer() test classes."""

    def setUp(self):
        live_cdn_signing._cached_signer = None
        self._env_patch = patch.dict(os.environ, FULL_SIGNING_ENV, clear=False)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        live_cdn_signing._cached_signer = None

    def test_success_returns_a_working_signer_and_caches_it(self):
        private_key, pem = _generate_signing_key_pem()
        fake_boto3 = _fake_boto3(sts_result=FAKE_STS_CREDENTIALS, secret_result={"SecretString": pem})

        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            first = get_configured_signer()
            second = get_configured_signer()

        self.assertIsNotNone(first)
        self.assertIs(first, second)
        # Exactly one "sts" + one "secretsmanager" client construction total
        # -- proves the second get_configured_signer() call reused the
        # cache instead of fetching from AWS again.
        self.assertEqual(fake_boto3.client.call_count, 2)

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = b"test-policy-bytes"
        signature = first(message)
        private_key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA1())

    def test_cached_signer_works_end_to_end_with_sign_segment_url(self):
        _private_key, pem = _generate_signing_key_pem()
        fake_boto3 = _fake_boto3(sts_result=FAKE_STS_CREDENTIALS, secret_result={"SecretString": pem})

        with patch.object(live_cdn_signing, "boto3", fake_boto3):
            rsa_signer = get_configured_signer()
            self.assertIsNotNone(rsa_signer)
            url = sign_segment_url(**{**VALID_KWARGS, "rsa_signer": rsa_signer})

        self.assertIn("&Signature=", url)
        self.assertIn("&Key-Pair-Id=KEYPAIRID123", url)


class CryptographyRsaSignerTests(unittest.TestCase):
    def test_produces_a_pkcs1v15_sha1_signature_verifiable_by_the_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = cryptography_rsa_signer(private_key)
        message = b"test-policy-bytes"
        signature = signer(message)

        # Verification success == no exception raised. No network, no real
        # AWS/CloudFront resource involved -- this proves the algorithm/API
        # choice is correct, independent of where the key ultimately comes
        # from in production.
        private_key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA1())

    def test_real_cloudfront_signer_end_to_end_with_a_throwaway_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = cryptography_rsa_signer(private_key)
        url = sign_segment_url(**{**VALID_KWARGS, "rsa_signer": signer})
        self.assertIn("&Signature=", url)
        self.assertIn("&Key-Pair-Id=KEYPAIRID123", url)


if __name__ == "__main__":
    unittest.main()
