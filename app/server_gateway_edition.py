"""Server-authorized edition selection for newly provisioned appliances."""


def onboarding_edition(payload, *, is_global_admin):
    """Only a live-authorized global administrator can choose Local Gateway."""
    edition = payload.get("gateway_edition")
    if edition is None:
        return None
    if is_global_admin is not True:
        raise PermissionError("Gateway edition requires a global administrator.")
    if edition != "local_gateway":
        raise ValueError("Unsupported Gateway edition.")
    return edition
