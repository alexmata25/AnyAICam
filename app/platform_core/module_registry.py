"""Module boundaries for incremental extraction from :mod:`app.main`.

The registry documents ownership before code is moved. Keeping this metadata in
Python makes it testable and usable by future migration tooling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ModuleBoundary:
    """A planned application module and the responsibilities it owns."""

    name: str
    package: str
    responsibilities: tuple[str, ...]
    forbidden_dependencies: tuple[str, ...] = ()


MODULES: Final[tuple[ModuleBoundary, ...]] = (
    ModuleBoundary(
        name="api",
        package="app.api",
        responsibilities=(
            "FastAPI routers and request/response schemas",
            "authentication and authorization dependencies",
            "customer, administrator, and partner HTTP endpoints",
        ),
        forbidden_dependencies=("camera vendor SDKs", "direct subprocess management"),
    ),
    ModuleBoundary(
        name="services",
        package="app.services",
        responsibilities=(
            "application use cases",
            "activation and account workflows",
            "notifications, storage, and recording orchestration",
        ),
        forbidden_dependencies=("FastAPI route decorators",),
    ),
    ModuleBoundary(
        name="edge",
        package="app.edge",
        responsibilities=(
            "appliance communication",
            "camera discovery and verification",
            "stream lifecycle and edge command coordination",
        ),
        forbidden_dependencies=("HTML rendering", "billing presentation logic"),
    ),
    ModuleBoundary(
        name="drivers",
        package="app.drivers",
        responsibilities=(
            "vendor and protocol adapters",
            "FFmpeg process construction",
            "object storage and database adapters",
        ),
        forbidden_dependencies=("FastAPI route decorators", "portal HTML"),
    ),
)


def validate_module_registry(
    modules: tuple[ModuleBoundary, ...] = MODULES,
) -> tuple[str, ...]:
    """Return registry validation errors without raising at import time."""

    errors: list[str] = []
    names: set[str] = set()
    packages: set[str] = set()

    for module in modules:
        if not module.name.strip():
            errors.append("module name must not be blank")
        elif module.name in names:
            errors.append(f"duplicate module name: {module.name}")
        names.add(module.name)

        if not module.package.startswith("app."):
            errors.append(f"package must be under app: {module.package}")
        elif module.package in packages:
            errors.append(f"duplicate package: {module.package}")
        packages.add(module.package)

        if not module.responsibilities:
            errors.append(f"module has no responsibilities: {module.name}")

    return tuple(errors)
