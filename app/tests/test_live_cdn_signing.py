"""Phase 6b (docs/AI_HANDOFF.md Sec 8): focused unit tests for
app/live_cdn_signing.py's sign_segment_url()/cryptography_rsa_signer()/
get_configured_signer().

Deliberately self-contained: no `main`/`partner_db` import (so no
test-discovery-order concern applies -- see test_live_view_page_
characterization.py's docstring for the pre-existing issue this avoids),
no real network/AWS call anywhere. botocore.signers.CloudFrontSigner's own
policy-building logic is exercised directly (not mocked) since it does no
I/O itself -- only the rsa_signer callback is ever faked/injected.
"""

import inspect
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
    def test_cloudfrontsigner_unavailable_raises_runtime_error(self):
        with patch.object(live_cdn_signing, "CloudFrontSigner", None):
            with self.assertRaises(RuntimeError):
                sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner()})

    def test_drift_guard_trips_if_live_relay_s3_prefix_diverges(self):
        with patch.object(live_cdn_signing, "live_relay_s3_prefix", return_value="something/else/entirely/"):
            with self.assertRaises(RuntimeError):
                sign_segment_url(**{**VALID_KWARGS, "rsa_signer": RecordingSigner()})

    def test_get_configured_signer_returns_none(self):
        self.assertIsNone(get_configured_signer())


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
