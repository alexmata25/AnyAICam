"""Fail-closed entry point for independently verified Gateway edition evidence.

The internal-test producer is unrouted. A reference is a lookup key, never
proof. Verification revalidates its stored authorization and audit on the
supplied transaction connection; the purchase category is unavailable.
"""
from dataclasses import dataclass

VERIFIED_GATEWAY_PURCHASE = 'verified_gateway_purchase'
AUTHORIZED_INTERNAL_PROVISIONING = 'authorized_internal_provisioning'
SOURCE_CATEGORIES = frozenset((VERIFIED_GATEWAY_PURCHASE, AUTHORIZED_INTERNAL_PROVISIONING))
EDITIONS = frozenset(('local_gateway', 'cloud_gateway'))


@dataclass(frozen=True)
class EvidenceResult:
    status: str
    reason: str

    def __bool__(self):
        return self.status == 'verified'


def verify_edition_evidence(db, *, source, reference, customer_id, appliance_id, edition, claim_id=None, version=None):
    """Verify internal test authorization; purchase evidence remains unavailable.

    ``db`` must be the caller's transaction connection. No external provider is contacted.
    A writer must require a genuinely verified result and revalidate it along
    with the claim and ownership in the same transaction as its audit.
    """
    if not isinstance(source, str) or source not in SOURCE_CATEGORIES:
        return EvidenceResult('invalid', 'Unsupported evidence category.')
    if not isinstance(edition, str) or edition not in EDITIONS:
        return EvidenceResult('invalid', 'Unsupported Gateway edition.')
    for value in (reference, customer_id, appliance_id):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return EvidenceResult('invalid', 'A source reference, customer and appliance are required.')
    if source == VERIFIED_GATEWAY_PURCHASE:
        return EvidenceResult('unavailable', 'No authoritative edition-specific purchase-to-appliance producer is connected.')
    if db is None or not isinstance(claim_id, str) or not claim_id or type(version) is not int:
        return EvidenceResult('unavailable', 'Internal test authorization, claim and version are required.')
    from gateway_internal_provisioning import verify_internal_test
    if verify_internal_test(db, reference, customer_id, appliance_id, claim_id, edition, version):
        return EvidenceResult('verified', 'Internal test authorization verified; this is not purchase evidence.')
    return EvidenceResult('invalid', 'Internal test authorization is missing, stale or invalid.')
