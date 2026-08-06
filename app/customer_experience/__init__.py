"""Tenant-scoped customer experience for AnyAiCam."""


def register_customer_experience_routes(*args, **kwargs):
    """Import web dependencies only when the application registers routes."""
    from customer_experience.routes import register_customer_experience_routes as register
    return register(*args, **kwargs)


__all__ = ["register_customer_experience_routes"]
