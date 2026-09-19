"""PortalClient.claim_begin/claim_status/claim_complete (Phase 1 of the
non-interactive/self-service claim flow -- see
docs/non-interactive-activation-phase1-plan.md and the cloud-side
app/appliance_claims.py these three methods talk to).

No existing test file exercises PortalClient's HTTP mechanics directly
(test()/activate() are only exercised indirectly, and not for their
wire format); these tests mock urllib.request.urlopen directly, the
same standard-library call PortalClient.request() itself makes, since
there's no closer existing pattern to match.

Also proves a correction to the original Phase 1 plan, found while
implementing this file: FORBIDDEN/sanitize() in portal.py scrubs
camera-credential-shaped keys OUT of the outbound request body before
it is ever sent -- it is a wire-safety net (see provisioning.py's own
comment calling it "a second, independent layer of defense"), not a
log scrubber. Adding claim_code/claim_proof to FORBIDDEN, as the
Phase 1 plan doc originally said this file would verify, would have
silently stripped those values from the real request bodies claim_
begin/claim_complete depend on, breaking the exchange outright. The
plan's actual intent -- these values must never be logged -- holds
regardless: none of the three methods below log anything, matching
test()/activate()'s own existing behavior exactly, so the values are
never at risk of appearing in a log line in the first place.
"""
import json
import urllib.error
from unittest.mock import MagicMock, patch

from anyaicam_agent.portal import FORBIDDEN, PortalClient, PortalError, sanitize


def _client():
    return PortalClient("https://portal.example.test")


def _mock_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


# --------------------------------------------------------- claim_begin


def test_claim_begin_posts_device_id_unauthenticated():
    client = _client()
    with patch("urllib.request.urlopen", return_value=_mock_response({"claim_session_id": "sess-1", "claim_code": "ABCD1234"})) as urlopen:
        result = client.claim_begin("AIC-DEVICE-0001")

    assert result == {"claim_session_id": "sess-1", "claim_code": "ABCD1234"}
    request = urlopen.call_args[0][0]
    assert request.full_url == "https://portal.example.test/api/appliance/claim/begin"
    assert request.get_header("Authorization") is None
    body = json.loads(request.data.decode())
    assert body == {"device_id": "AIC-DEVICE-0001"}


def test_claim_begin_raises_portal_error_with_status_code_on_http_error():
    client = _client()
    error_body = json.dumps({"detail": "Claim attempt rate exceeded."}).encode()
    http_error = urllib.error.HTTPError(url="https://portal.example.test/api/appliance/claim/begin", code=429, msg="Too Many Requests", hdrs=None, fp=None)
    http_error.read = lambda: error_body
    with patch("urllib.request.urlopen", side_effect=http_error):
        try:
            client.claim_begin("AIC-DEVICE-0001")
            assert False, "expected PortalError"
        except PortalError as error:
            assert error.status_code == 429
            assert "rate exceeded" in str(error)


# --------------------------------------------------------- claim_status


def test_claim_status_posts_claim_session_id_not_as_a_url_param():
    client = _client()
    with patch("urllib.request.urlopen", return_value=_mock_response({"status": "pending"})) as urlopen:
        result = client.claim_status("sess-1")

    assert result == {"status": "pending"}
    request = urlopen.call_args[0][0]
    # The whole point of this endpoint being POST, not GET-with-query-
    # param, is that claim_session_id never appears in a URL (see
    # appliance_claims.py's own comment on why) -- assert that directly.
    assert "sess-1" not in request.full_url
    assert json.loads(request.data.decode()) == {"claim_session_id": "sess-1"}


def test_claim_status_returns_proof_when_present():
    client = _client()
    with patch("urllib.request.urlopen", return_value=_mock_response({"status": "claimed", "claim_proof": "proof-value"})):
        result = client.claim_status("sess-1")

    assert result["claim_proof"] == "proof-value"


# --------------------------------------------------------- claim_complete


def test_claim_complete_posts_session_and_proof_not_in_url():
    client = _client()
    with patch("urllib.request.urlopen", return_value=_mock_response({"appliance_id": "appl-1", "cloud_id": "AIC-DEVICE-0001", "credential": "cred-value"})) as urlopen:
        result = client.claim_complete("sess-1", "proof-value")

    assert result["credential"] == "cred-value"
    request = urlopen.call_args[0][0]
    assert "sess-1" not in request.full_url and "proof-value" not in request.full_url
    body = json.loads(request.data.decode())
    assert body == {"claim_session_id": "sess-1", "claim_proof": "proof-value"}


def test_claim_complete_replay_surfaces_conflict_status_code():
    client = _client()
    error_body = json.dumps({"detail": "Claim proof was already used."}).encode()
    http_error = urllib.error.HTTPError(url="https://portal.example.test/api/appliance/claim/complete", code=409, msg="Conflict", hdrs=None, fp=None)
    http_error.read = lambda: error_body
    with patch("urllib.request.urlopen", side_effect=http_error):
        try:
            client.claim_complete("sess-1", "already-used-proof")
            assert False, "expected PortalError"
        except PortalError as error:
            assert error.status_code == 409


# --------------------------------------------------------- sanitize()/FORBIDDEN correction


def test_forbidden_set_does_not_strip_claim_flow_values():
    # Regression test for the plan-doc correction described in this
    # file's module docstring: claim_code/claim_proof/claim_session_id
    # (and credential/activation_token, the existing values this same
    # reasoning already applied to) must survive sanitize() unchanged,
    # because they are legitimate wire values this exchange depends on
    # -- not camera-credential-shaped keys that should never reach the
    # portal through this generic path at all.
    payload = {
        "claim_code": "ABCD1234",
        "claim_proof": "proof-value",
        "claim_session_id": "sess-1",
        "credential": "cred-value",
        "activation_token": "token-value",
    }

    assert sanitize(payload) == payload
    assert not (FORBIDDEN & payload.keys())


def test_forbidden_set_still_strips_camera_credential_shaped_keys():
    payload = {"device_id": "AIC-DEVICE-0001", "username": "admin", "password": "hunter2", "rtsp_url": "rtsp://x"}

    result = sanitize(payload)

    assert result == {"device_id": "AIC-DEVICE-0001"}
