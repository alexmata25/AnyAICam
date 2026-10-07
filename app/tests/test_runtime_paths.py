"""app/runtime_paths.py (2026-10-07, Windows native runtime): the VMS's data and
static roots. Unset (every Linux appliance) they are exactly the container's
/app/recordings and /app/static; a Windows install points them elsewhere.

Each case imports the modules in a fresh interpreter so the environment it
sets cannot leak into other tests (the constants are read at import)."""
import json
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import runtime_paths, business_portal, pricing_config, pricing_portal, mobile_notifications, cloud_config, facial_people
print(json.dumps({
    "recordings": str(runtime_paths.RECORDINGS_ROOT),
    "static": str(runtime_paths.STATIC_ROOT),
    "default_db": runtime_paths.recordings_default("partner_portal.db"),
    "business": str(business_portal.DATA_FILE),
    "pricing": str(pricing_config.CONFIG_FILE),
    "quotes": str(pricing_portal.QUOTES_FILE),
    "mobile": str(mobile_notifications.RECORDINGS_FOLDER),
    "sqlite": cloud_config.settings.sqlite_path,
    "storage": cloud_config.settings.local_storage_root,
    "faces": str(facial_people.AAC_FACES_FOLDER),
}))
'''


def _probe(tmp_path, **env):
    environment = {k: v for k, v in os.environ.items() if k not in (
        "ANYAICAM_RECORDINGS_FOLDER", "ANYAICAM_STATIC_FOLDER", "ANYAICAM_PARTNER_DB", "ANYAICAM_LOCAL_STORAGE_ROOT",
        "ANYAICAM_AAC_FACES_FOLDER")}
    environment.update(env)
    # Importing these modules must never touch a real database: point the DB
    # at a throwaway file unless the case under test sets the data root.
    environment.setdefault("ANYAICAM_PARTNER_DB", str(tmp_path / "probe.db")) if "ANYAICAM_RECORDINGS_FOLDER" not in env else None
    result = subprocess.run([sys.executable, "-c", PROBE, str(APP)], env=environment, cwd=tmp_path,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_unset_roots_are_exactly_the_linux_container_paths(tmp_path):
    paths = _probe(tmp_path)
    assert Path(paths["recordings"]) == Path("/app/recordings")
    assert Path(paths["static"]) == Path("/app/static")
    assert paths["default_db"] == "/app/recordings/partner_portal.db"  # same literal as before
    assert Path(paths["business"]) == Path("/app/recordings/account_management.json")
    assert Path(paths["pricing"]) == Path("/app/recordings/pricing_config.json")
    assert Path(paths["quotes"]) == Path("/app/recordings/customer_quotes.json")
    assert Path(paths["mobile"]) == Path("/app/recordings")
    assert paths["storage"] == "/app/recordings/storage"
    assert Path(paths["faces"]) == Path("/app/recordings/aac_faces")


def test_windows_style_roots_move_every_data_file(tmp_path):
    data, static = tmp_path / "ProgramData" / "AnyAiCam" / "data", tmp_path / "Program Files" / "AnyAiCam" / "app" / "static"
    paths = _probe(tmp_path, ANYAICAM_RECORDINGS_FOLDER=str(data), ANYAICAM_STATIC_FOLDER=str(static))
    assert Path(paths["recordings"]) == data and Path(paths["static"]) == static
    for key, name in (("default_db", "partner_portal.db"), ("business", "account_management.json"),
                      ("pricing", "pricing_config.json"), ("quotes", "customer_quotes.json"),
                      ("sqlite", "partner_portal.db"), ("storage", "storage"), ("faces", "aac_faces")):
        assert Path(paths[key]) == data / name, key
    assert Path(paths["mobile"]) == data


def test_an_explicit_per_file_setting_still_wins(tmp_path):
    data = tmp_path / "data"
    faces = tmp_path / "elsewhere" / "faces"
    paths = _probe(tmp_path, ANYAICAM_RECORDINGS_FOLDER=str(data), ANYAICAM_AAC_FACES_FOLDER=str(faces))
    assert Path(paths["faces"]) == faces


def test_blank_setting_means_unset(tmp_path):
    paths = _probe(tmp_path, ANYAICAM_STATIC_FOLDER="   ")
    assert Path(paths["static"]) == Path("/app/static")


def test_no_module_hard_codes_the_container_roots_any_more():
    offenders = []
    for path in APP.glob("*.py"):
        if path.name in ("runtime_paths.py",) or path.name.endswith("_override.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for literal in ('Path("/app/recordings")', "Path('/app/recordings')", 'Path("/app/static")',
                        'directory="/app/static"', 'directory="/app/recordings"', "'/app/recordings/", '"/app/recordings/'):
            if literal in text:
                offenders.append(f"{path.name}: {literal}")
    assert offenders == []
