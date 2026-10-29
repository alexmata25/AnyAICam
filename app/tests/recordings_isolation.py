"""Per-test isolation of every app path that defaults into the shared recordings root (Codex note on 108a132).

runtime_paths.RECORDINGS_ROOT (C:\\app\\recordings on a Windows dev box when ANYAICAM_RECORDINGS_FOLDER is unset)
is resolved at import, and dozens of module constants derive from it: the legacy audit log, in-app alerts, AAC face
and face-event folders, the appliance identity file, email previews, license state, users/sessions files... A plain
`pytest` run (no isolating runner) therefore reads and writes the shared folder, including any stray
appliance_identity.json left there. isolate_recordings() redirects all of them to tmp_path for one test:

  * every Path / absolute-path str global under the recordings root in an already-imported app module;
  * every such str field of a module-level dataclass `settings` object;
  * the ANYAICAM_* environment variables, for modules imported lazily later in the test.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

_ENV_PATHS = {
    "ANYAICAM_RECORDINGS_FOLDER": (),
    "ANYAICAM_AAC_FACES_FOLDER": ("aac_faces",),
    "ANYAICAM_AAC_THUMBNAIL_FOLDER": ("aac_faces", "events"),
    "ANYAICAM_APPLIANCE_IDENTITY_FILE": ("appliance_identity.json",),
    "ANYAICAM_EMAIL_PREVIEW_DIR": ("email-preview",),
}


def _under(value, roots) -> Path | None:
    try:
        candidate = Path(value)
    except TypeError:
        return None
    if not candidate.is_absolute():
        return None
    for root in roots:
        try:
            return candidate.relative_to(root)
        except ValueError:
            continue
    return None


# Isolation targets handed out earlier in this process. A module imported lazily DURING an earlier isolated test
# computes its path constants from that test's ANYAICAM_* env (monkeypatch cannot undo an import-time constant),
# so those stale paths must be redirected too.
_PREVIOUS_TARGETS: set = set()


def isolate_recordings(monkeypatch, tmp_path) -> Path:
    import runtime_paths

    target = Path(tmp_path) / "isolated-recordings"
    target.mkdir(parents=True, exist_ok=True)
    roots = {Path(runtime_paths.RECORDINGS_ROOT), Path(r"C:\app\recordings"), Path("/app/recordings"), *_PREVIOUS_TARGETS}
    _PREVIOUS_TARGETS.add(target)
    app_dir = Path(runtime_paths.__file__).resolve().parent
    tests_dir = app_dir / "tests"
    for module in list(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if not source:
            continue
        source_path = Path(source).resolve()
        if app_dir not in source_path.parents or tests_dir in source_path.parents:
            continue
        for attribute, value in list(vars(module).items()):
            if attribute.startswith("__") or not isinstance(value, (str, Path)):
                continue
            if isinstance(value, str) and (len(value) > 400 or "\n" in value):
                continue
            relative = _under(value, roots)
            if relative is not None:
                monkeypatch.setattr(module, attribute, type(value)(target / relative) if isinstance(value, Path) else str(target / relative))
        settings = getattr(module, "settings", None)
        if dataclasses.is_dataclass(settings) and not isinstance(settings, type):
            changes = {}
            for field in dataclasses.fields(settings):
                value = getattr(settings, field.name)
                relative = _under(value, roots) if isinstance(value, (str, Path)) else None
                if relative is not None:
                    changes[field.name] = type(value)(target / relative) if isinstance(value, Path) else str(target / relative)
            if changes:
                monkeypatch.setattr(module, "settings", dataclasses.replace(settings, **changes))
    monkeypatch.setattr(runtime_paths, "RECORDINGS_ROOT", target)
    for variable, parts in _ENV_PATHS.items():
        monkeypatch.setenv(variable, str(target.joinpath(*parts)))
    return target


def assert_isolated(target: Path) -> None:
    """The writers these tests reach must point inside target, never at the shared recordings root."""
    import facial_events
    import facial_people
    import main
    checked = {
        "main.RECORDINGS_FOLDER": main.RECORDINGS_FOLDER,
        "main.AUDIT_LOG_FILE": main.AUDIT_LOG_FILE,
        "main.IN_APP_ALERTS_FILE": main.IN_APP_ALERTS_FILE,
        "facial_people.AAC_FACES_FOLDER": facial_people.AAC_FACES_FOLDER,
        "facial_events.AAC_THUMBNAIL_FOLDER": facial_events.AAC_THUMBNAIL_FOLDER,
    }
    try:
        import appliance_activation
        checked["appliance_activation.ACTIVATION_IDENTITY_FILE"] = appliance_activation.ACTIVATION_IDENTITY_FILE
    except Exception:  # pragma: no cover - module absent on some lines
        pass
    for name, value in checked.items():
        assert target in Path(value).parents or Path(value) == target, (name, value)
