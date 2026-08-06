"""Multi-tenant identity, authorization, onboarding, and persistence."""

from tenancy.policy import (
    CUSTOMER_ROLES,
    PLATFORM_ROLES,
    authorize,
    authorize_camera,
    identity_domain,
    normalize_role,
)

__all__ = [
    "CUSTOMER_ROLES",
    "PLATFORM_ROLES",
    "authorize",
    "authorize_camera",
    "identity_domain",
    "normalize_role",
]

