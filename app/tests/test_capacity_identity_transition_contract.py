"""Pure contract tests for signed cloud-to-edge capacity identity changes."""

import pytest

import feature_entitlements


def test_initial_claim_transition_binds_factory_id_to_exact_cloud_identity():
    appliance = {"id": "cloud-appliance", "cloud_id": "AIC-device", "customer_id": "customer-1"}
    transition = feature_entitlements.initial_capacity_identity_transition("factory-uuid", appliance)

    assert transition["schema"] == 1
    assert transition["kind"] == "initial_claim"
    assert transition["from_appliance_id"] == "factory-uuid"
    assert transition["to_identity"] == {
        "appliance_id": "cloud-appliance", "cloud_id": "AIC-device", "customer_id": "customer-1",
    }
    assert transition["transition_id"]


def test_reenrollment_transition_requires_complete_source_and_supported_reason():
    source = {"appliance_id": "cloud-appliance", "cloud_id": "AIC-device", "customer_id": "customer-1"}
    target = {"id": "cloud-appliance-2", "cloud_id": "AIC-device-2", "customer_id": "customer-2"}

    transition = feature_entitlements.authorized_capacity_identity_transition(
        source, target, reason="ownership_transfer")

    assert transition["schema"] == 1
    assert transition["kind"] == "authorized"
    assert transition["reason"] == "ownership_transfer"
    assert transition["from_identity"] == source
    assert transition["to_identity"] == {
        "appliance_id": "cloud-appliance-2", "cloud_id": "AIC-device-2", "customer_id": "customer-2",
    }
    assert transition["authorized_at"]
    with pytest.raises(ValueError):
        feature_entitlements.authorized_capacity_identity_transition(source, target, reason="unspecified")
    with pytest.raises(ValueError):
        feature_entitlements.authorized_capacity_identity_transition(
            {"appliance_id": source["appliance_id"]}, target, reason="reenrollment")
