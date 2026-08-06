import unittest

from platform_core.module_registry import (
    MODULES,
    ModuleBoundary,
    validate_module_registry,
)


class ModuleRegistryTests(unittest.TestCase):
    def test_default_registry_is_valid(self):
        self.assertEqual(validate_module_registry(), ())

    def test_expected_packages_are_registered(self):
        self.assertEqual(
            {module.package for module in MODULES},
            {"app.api", "app.services", "app.edge", "app.drivers"},
        )

    def test_duplicate_names_and_packages_are_rejected(self):
        duplicate = (
            ModuleBoundary("api", "app.api", ("routes",)),
            ModuleBoundary("api", "app.api", ("duplicate",)),
        )
        errors = validate_module_registry(duplicate)
        self.assertIn("duplicate module name: api", errors)
        self.assertIn("duplicate package: app.api", errors)

    def test_packages_must_remain_under_app(self):
        errors = validate_module_registry(
            (ModuleBoundary("bad", "external.bad", ("invalid",)),)
        )
        self.assertIn("package must be under app: external.bad", errors)


if __name__ == "__main__":
    unittest.main()
