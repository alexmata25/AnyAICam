"""1.3.2 Sandbox acceptance (2026-10-21, S-L on 17cf93c): the harness's AppPython helper reads the installed VMS state
in its own Python process. It must resolve the same data folders as the service, or it reads a different appliance
identity (C:\\app\\recordings) and reports every accepted capacity as null although the service accepted it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"

# service-launcher.ps1 location -> the same location written with the harness's $app (install root) / $data (ProgramData\AnyAiCam)
SAME_PLACE = {
    "ANYAICAM_STATIC_FOLDER": ("Join-Path $installRoot 'app\\static'", r"$app\app\static"),
    "ANYAICAM_RECORDINGS_FOLDER": ("Join-Path $dataRoot 'recordings'", r"$data\recordings"),
    "ANYAICAM_HLS_FOLDER": ("Join-Path $dataRoot 'hls'", r"$data\hls"),
    "ANYAICAM_PARTNER_DB": ("Join-Path $dataRoot 'database\\partner_portal.db'", r"$data\database\partner_portal.db"),
    "ANYAICAM_STATE_DIR": ("Join-Path $dataRoot 'agent'", r"$data\agent"),
}


def _app_python_prelude() -> str:
    script = (WIN / "sandbox" / "acceptance-127.ps1").read_text(encoding="utf-8-sig")
    function = script.split("function AppPython([string]$Code) {", 1)[1]
    return function.split('"@', 1)[0]


def test_the_harness_sets_every_data_folder_the_service_launcher_sets_to_the_same_place():
    launcher = (WIN / "service-launcher.ps1").read_text(encoding="utf-8-sig")
    prelude = _app_python_prelude()
    for variable, (service, harness) in SAME_PLACE.items():
        assert f"$env:{variable} = {service}" in launcher, variable
        assert f"os.environ['{variable}'] = r'{harness}'" in prelude, variable


def test_no_runtime_paths_folder_variable_is_left_to_the_linux_default():
    launcher = (WIN / "service-launcher.ps1").read_text(encoding="utf-8-sig")
    runtime_paths = (ROOT / "app" / "runtime_paths.py").read_text(encoding="utf-8")
    for variable in re.findall(r"_root\('(ANYAICAM_[A-Z_]+)'", runtime_paths):
        assert variable in SAME_PLACE and f"$env:{variable} =" in launcher, variable


def test_the_harness_identity_file_is_the_one_the_service_reads():
    script = (WIN / "sandbox" / "acceptance-127.ps1").read_text(encoding="utf-8-sig")
    assert "$identityFile = Join-Path $data 'recordings\\appliance_identity.json'" in script
    activation = (ROOT / "app" / "appliance_activation.py").read_text(encoding="utf-8")
    assert 'runtime_paths.recordings_default("appliance_identity.json")' in activation
