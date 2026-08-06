"""Separate platform and customer navigation domains."""

from tenancy.policy import identity_domain, normalize_role


PLATFORM_NAVIGATION = {
    "owner": None,
    "sales": {"partner", "partner-sales", "partner-quotes", "pricing", "new-customer", "help"},
    "support": {"admin-portal", "admin-customers", "admin-support", "audit", "help"},
    "installer": {"partner-install", "camera-provisioning", "camera-health", "sites", "help"},
    "billing": {"admin-customers", "billing-operations", "subscription-admin", "license-management", "help"},
    "operations": {"dashboard", "operations", "camera-health", "appliances", "release-readiness", "help"},
}

CUSTOMER_NAVIGATION = {
    "customer_admin": {"live", "events", "alerts", "playback", "media", "dashboard", "settings", "tenant-camera-sharing", "subscription-portal", "phone", "help"},
    "manager": {"live", "events", "alerts", "playback", "media", "dashboard", "settings", "phone", "help"},
    "viewer": {"live", "events", "alerts", "playback", "dashboard", "phone", "help"},
    "guard": {"live", "events", "alerts", "phone", "help"},
}


def navigation_keys(identity: dict) -> set[str] | None:
    domain = identity_domain(identity)
    _, role = normalize_role(identity)
    return (PLATFORM_NAVIGATION if domain == "platform" else CUSTOMER_NAVIGATION).get(role, {"help"})
