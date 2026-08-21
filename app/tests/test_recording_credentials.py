"""R1: focused tests for recording_credentials.py's pure functions --
no AWS/boto3 involved, no app/main.py import (avoids the documented
test-discovery-order fragility from partner_db's import-time schema
init). Proves tenant isolation at the code level: two different
camera_ids never produce the same prefix, and the generated IAM
session policy never grants anything beyond a single s3:PutObject on
that exact camera's own prefix.
"""

from recording_credentials import (
    RECORDING_SESSION_DURATION_SECONDS,
    recording_s3_prefix,
    recording_session_name,
    recording_session_policy,
)


def test_prefix_shape():
    prefix = recording_s3_prefix('cust-1', 'site-1', 'appl-1', 'cam-1')
    assert prefix == 'recordings/cust-1/site-1/appl-1/cam-1/'


def test_prefix_isolated_per_camera():
    a = recording_s3_prefix('cust-1', 'site-1', 'appl-1', 'cam-A')
    b = recording_s3_prefix('cust-1', 'site-1', 'appl-1', 'cam-B')
    assert a != b
    assert not a.startswith(b)
    assert not b.startswith(a)


def test_prefix_isolated_per_customer():
    a = recording_s3_prefix('cust-1', 'site-1', 'appl-1', 'cam-1')
    b = recording_s3_prefix('cust-2', 'site-1', 'appl-1', 'cam-1')
    assert a != b


def test_policy_grants_only_put_object_on_the_camera_own_prefix():
    policy = recording_session_policy('my-bucket', 'cust-1', 'site-1', 'appl-1', 'cam-1')
    assert policy['Version'] == '2012-10-17'
    assert len(policy['Statement']) == 1
    statement = policy['Statement'][0]
    assert statement['Effect'] == 'Allow'
    assert statement['Action'] == 's3:PutObject'
    assert statement['Resource'] == 'arn:aws:s3:::my-bucket/recordings/cust-1/site-1/appl-1/cam-1/*'


def test_policy_never_grants_list_get_or_delete():
    policy = recording_session_policy('my-bucket', 'cust-1', 'site-1', 'appl-1', 'cam-1')
    actions = {statement['Action'] for statement in policy['Statement']}
    assert actions == {'s3:PutObject'}


def test_two_cameras_get_non_overlapping_policies():
    policy_a = recording_session_policy('my-bucket', 'cust-1', 'site-1', 'appl-1', 'cam-A')
    policy_b = recording_session_policy('my-bucket', 'cust-1', 'site-1', 'appl-1', 'cam-B')
    resource_a = policy_a['Statement'][0]['Resource']
    resource_b = policy_b['Statement'][0]['Resource']
    assert resource_a != resource_b
    assert not resource_a.startswith(resource_b.rstrip('*'))
    assert not resource_b.startswith(resource_a.rstrip('*'))


def test_session_name_sanitized_and_bounded():
    name = recording_session_name('appl-1', 'cam-1', now=1700000000)
    assert name == 'rec-appl-1-cam-1-1700000000'
    assert len(name) <= 64


def test_session_name_strips_unsafe_characters():
    name = recording_session_name('appl/1 weird', 'cam#1', now=1700000000)
    assert set(name) <= set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+=,.@-')


def test_session_duration_matches_live_relay_precedent():
    assert RECORDING_SESSION_DURATION_SECONDS == 900
