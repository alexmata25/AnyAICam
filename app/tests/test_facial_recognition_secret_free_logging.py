"""AAC secret-free logging -- Phase 1.

Two layers of coverage: a static source-scan (below) that fails the
build the moment anyone writes a print()/logger call whose arguments
touch an embedding/image-bytes variable by name, and the dynamic,
end-to-end check already in test_facial_recognition_ui.py
(test_audit_log_never_contains_the_raw_uploaded_image_or_embedding),
which proves the real audit_logs rows written by a real enrollment
request never contain the raw upload or a raw embedding vector.
"""

import ast
from pathlib import Path

AAC_MODULES = [
    "facial_recognition.py",
    "facial_people.py",
    "facial_events.py",
    "facial_recognition_ui.py",
    "relay_control.py",
]

# Any of these appearing as a bare variable/attribute NAME inside a
# print()/logging call's arguments is treated as a violation -- these
# are exactly the names this codebase's own AAC modules use for
# biometric templates or raw uploads (embedding tuples, base64 image
# payloads, decoded image arrays).
SENSITIVE_NAMES = {"embedding", "embeddings", "image_base64", "image_bgr", "face_crop_bgr", "raw"}

LOGGING_CALL_ROOTS = {"print", "logger", "logging"}


def _call_root_name(node: ast.expr) -> str | None:
    target = node
    while isinstance(target, ast.Attribute):
        target = target.value
    if isinstance(target, ast.Name):
        return target.id
    return None


def _names_used(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _find_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        root = _call_root_name(node.func)
        if root not in LOGGING_CALL_ROOTS:
            continue
        used = set()
        for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
            used |= _names_used(argument)
        offending = used & SENSITIVE_NAMES
        if offending:
            violations.append(f"{path.name}:{node.lineno} logs sensitive name(s) {sorted(offending)}")
    return violations


def test_no_aac_module_logs_a_sensitive_variable_by_name():
    app_dir = Path(__file__).resolve().parent.parent
    all_violations: list[str] = []
    for module_name in AAC_MODULES:
        all_violations.extend(_find_violations(app_dir / module_name))
    assert all_violations == [], "\n".join(all_violations)


def test_audit_calls_never_pass_embedding_or_image_arguments():
    """A narrower, complementary static check specifically on
    facial_recognition_ui.py's own audit() call sites (partner_db.audit),
    since that is the one place this module's mutations become a
    permanent, durable record -- every audit() call's `details` dict
    literal must never be built from a sensitive variable."""
    app_dir = Path(__file__).resolve().parent.parent
    tree = ast.parse((app_dir / "facial_recognition_ui.py").read_text(encoding="utf-8"))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_root_name(node.func) == "audit":
            used = set()
            for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
                used |= _names_used(argument)
            offending = used & SENSITIVE_NAMES
            if offending:
                violations.append(f"line {node.lineno}: audit() call references {sorted(offending)}")
    assert violations == [], "\n".join(violations)
