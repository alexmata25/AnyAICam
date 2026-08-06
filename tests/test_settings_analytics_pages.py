import ast
from html import escape
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = ROOT / "app" / "main.py"


class RouteStub:
    def get(self, *_args, **_kwargs):
        return lambda function: function


class SettingsAnalyticsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def node(self, kind, name):
        return next(
            item
            for item in self.tree.body
            if isinstance(item, kind) and getattr(item, "name", None) == name
        )

    def assignment(self, name):
        return next(
            item
            for item in self.tree.body
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in item.targets)
        )

    def execute(self, *nodes, **values):
        namespace = {
            "app": RouteStub(),
            "HTMLResponse": object,
            "Request": object,
            **values,
        }
        module = ast.fix_missing_locations(ast.Module(body=list(nodes), type_ignores=[]))
        exec(compile(module, str(MAIN_SOURCE), "exec"), namespace)
        return namespace

    def test_settings_page_renders_category_links(self):
        namespace = self.execute(
            self.node(ast.FunctionDef, "slugify"),
            self.assignment("SETTINGS_CATEGORIES"),
            self.node(ast.FunctionDef, "settings"),
            re=re,
            escape=escape,
            current_user=lambda _request: {"role": "administrator"},
            has_permission=lambda _user, _permission: True,
            record_audit=lambda *_args: None,
            page_shell=lambda _title, _active, content, _scripts="": content,
        )

        html = namespace["settings"](object())
        self.assertIn('href="/settings/cameras"', html)
        self.assertIn('href="/settings/events-alerts"', html)

    def test_analytics_page_renders_feature_links(self):
        namespace = self.execute(
            self.assignment("ANALYTICS_FEATURES"),
            self.node(ast.FunctionDef, "slugify"),
            self.node(ast.FunctionDef, "analytics"),
            re=re,
            escape=escape,
            CAMERA_COUNT=4,
            page_shell=lambda _title, _active, content, _scripts="": content,
        )

        html = namespace["analytics"]()
        self.assertIn('href="/analytics/license-plate-recognition"', html)
        self.assertIn('href="/analytics/vehicle-search"', html)


if __name__ == "__main__":
    unittest.main()
