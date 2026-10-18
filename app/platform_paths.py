"""Fixed locations of the appliance's trust anchors, per platform -- never taken from the environment.

The Software Update trust directory holds the entitlement signing keyset and the accepted / fallback VMS capacity.
On Linux it is root-owned /etc/anyaicam-update (unchanged). On Windows both services run as LocalSystem and the
installer restricts %ProgramData%\\AnyAiCam to SYSTEM and Administrators; the trust directory is
%ProgramData%\\AnyAiCam\\update, with %ProgramData% resolved through the Windows known-folder API (an environment
variable could be set by an unprivileged user).

windows_write_problem() is the Windows counterpart of appliance_entitlements.ownership_problem(): an entry is safe
only when it is not a reparse point (symlink / junction), its owner is SYSTEM or Administrators, and no allow-ACE
grants a modifying right to anyone else.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

WINDOWS = os.name == "nt"


def _windows_program_data() -> Path:
    """%ProgramData% via SHGetKnownFolderPath(FOLDERID_ProgramData); C:\\ProgramData if the API is unavailable."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        folder = GUID(0x62AB5D82, 0xFDC1, 0x4DC3, (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97))
        found = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(found)) == 0:
            try:
                return Path(found.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(found)
    except Exception:
        pass
    return Path(r"C:\ProgramData")


if WINDOWS:
    _ROOT = _windows_program_data() / "AnyAiCam"
    UPDATE_DIR = _ROOT / "update"
    CONFIG_DIR = _ROOT / "config"  # the Windows data root is %ProgramData%\AnyAiCam itself (AnyAiCam-VMS.iss)
else:
    UPDATE_DIR = Path("/etc/anyaicam-update")
    CONFIG_DIR = Path("/opt/anyaicam/data/config")

# ---------------------------------------------------------------- Windows ACL check (pure parser + ctypes reader)
_TRUSTED_SIDS = {"SY", "BA", "S-1-5-18", "S-1-5-32-544"}
# SDDL right tokens that modify a file/directory, its attributes, ACL, owner or children.
_WRITE_TOKENS = {"GA", "GW", "FA", "FW", "WD", "WO", "SD", "DC", "CC", "WP", "KA", "KW"}
# FILE_WRITE_DATA|APPEND|WRITE_EA|DELETE_CHILD|WRITE_ATTRIBUTES|DELETE|WRITE_DAC|WRITE_OWNER|GENERIC_ALL|GENERIC_WRITE
_WRITE_MASK = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
_ACE = re.compile(r"\(([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^;()]*)(?:;[^()]*)?\)")


def _grants_write(rights: str) -> bool:
    rights = rights.strip()
    if rights.lower().startswith("0x"):
        try:
            return bool(int(rights, 16) & _WRITE_MASK)
        except ValueError:
            return True  # unparseable: treat as unsafe
    return any(rights[i:i + 2] in _WRITE_TOKENS for i in range(0, len(rights), 2))


def sddl_write_problem(sddl: str) -> str:
    """Why this security descriptor (SDDL) is not safe for a trust anchor, or "" when only SYSTEM/Administrators
    own it and can modify it. Inherit-only ACEs (IO) do not apply to the object itself."""
    owner = re.search(r"O:([^:]+?)(?=[GDS]:|$)", sddl)
    if not owner or owner.group(1) not in _TRUSTED_SIDS:
        return f"is owned by {owner.group(1) if owner else 'nobody'}, not SYSTEM or Administrators"
    dacl = re.search(r"D:([^()]*)((?:\([^()]*\))*)", sddl)
    if not dacl:
        return "has no access list (anyone could write it)"
    if "P" not in dacl.group(1) and "AI" not in dacl.group(1) and not dacl.group(2):
        return "has an empty access list"
    for ace_type, flags, rights, _object, _inherit, sid in _ACE.findall(dacl.group(2)):
        if ace_type not in ("A", "OA") or "IO" in flags:
            continue
        if sid not in _TRUSTED_SIDS and _grants_write(rights):
            return f"is writable by {sid}"
    return ""


def _windows_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.windll.advapi32
    info = 0x1 | 0x4  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(path), info, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise OSError(f"cannot read the security descriptor of {path}")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetFileSecurityW(str(path), info, buffer, needed, ctypes.byref(needed)):
        raise OSError(f"cannot read the security descriptor of {path}")
    text = ctypes.c_wchar_p()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, info, ctypes.byref(text), None):
        raise OSError(f"cannot convert the security descriptor of {path}")
    try:
        return text.value or ""
    finally:
        ctypes.windll.kernel32.LocalFree(text)


def windows_write_problem(path: Path) -> str:
    """Why this Windows file/directory is not a safe trust-anchor entry, or "" (see the module docstring)."""
    info = os.lstat(path)
    if getattr(info, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return "is a reparse point (link)"
    return sddl_write_problem(_windows_sddl(path))
