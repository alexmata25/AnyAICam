"""The entitlement trust anchor on a native Windows install (2026-10-08).

appliance_entitlements trusts paid-feature snapshots only when they are
signed by a key in the keyset a release installed. On Linux that keyset is
root-owned under /etc/anyaicam-update/. On Windows that path meant
C:\\etc\\anyaicam-update\\..., which nothing installs (so every paid feature
was denied) and which an ordinary user may create (C:\\ lets authenticated
users create folders) -- and the POSIX-only ownership check passed anything
on Windows. A standard user could have planted a keyset of their own and
signed their own Talk Down / AAC Voice Call grants.

On Windows the keyset is:

    %ProgramData%\\AnyAiCam\\trust\\entitlement_signing_keys.json

%ProgramData% comes from the known-folder API (FOLDERID_ProgramData), never
from the environment. The setup installs the file (install-runtime.ps1,
same validate_entitlement_keyset rules and release digest as Linux) and
makes the folder and the file owned by Administrators, with a protected
access list: SYSTEM and Administrators only.

anchor_problem() returns why the keyset may not be trusted, or "". For the
file and each of its two parent folders (trust, AnyAiCam):
  * not a reparse point (symbolic link, junction, mount point) -- a
    redirected path is refused, not followed;
  * the right kind of object (folder / regular file);
  * owned by SYSTEM, Administrators or TrustedInstaller;
  * a DACL is present (a NULL DACL grants everyone everything);
  * no allow entry that applies to the object gives any other account a
    right to change it: write/append data or add files/folders, write
    attributes or extended attributes, delete or delete a child, change
    permissions, take ownership, or the generic write/all rights. Deny
    entries only take rights away and are not needed for the check;
    entries that only apply to future children (inherit-only) are ignored
    because every existing level is checked itself. An allow entry this
    module cannot parse makes the whole keyset untrusted.
Anything that cannot be checked is a problem: the keyset is then not
trusted and paid features are denied (recording, live view and the rest of
the VMS never consult it).
"""
from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from pathlib import Path

KEYSET_NAME = "entitlement_signing_keys.json"

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
OWNER_RIGHTS_SID = "S-1-3-4"  # the (trusted) owner's own rights
TRUSTED_SIDS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID})

# Rights that let a holder change, replace or redirect the object.
FILE_WRITE_DATA = 0x0002          # = FILE_ADD_FILE on a folder
FILE_APPEND_DATA = 0x0004         # = FILE_ADD_SUBDIRECTORY on a folder
FILE_WRITE_EA = 0x0010
FILE_DELETE_CHILD = 0x0040
FILE_WRITE_ATTRIBUTES = 0x0100
DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
MAXIMUM_ALLOWED = 0x02000000
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000
WRITE_RIGHTS = (FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_DELETE_CHILD | FILE_WRITE_ATTRIBUTES
                | DELETE | WRITE_DAC | WRITE_OWNER | MAXIMUM_ALLOWED | GENERIC_ALL | GENERIC_WRITE)

ACCESS_ALLOWED_ACE_TYPE = 0x0
ACCESS_DENIED_ACE_TYPE = 0x1
ACCESS_ALLOWED_CALLBACK_ACE_TYPE = 0x9
ACCESS_DENIED_CALLBACK_ACE_TYPE = 0xA
ACCESS_DENIED_OBJECT_ACE_TYPE = 0x6
ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE = 0xC
DENY_TYPES = frozenset({ACCESS_DENIED_ACE_TYPE, ACCESS_DENIED_CALLBACK_ACE_TYPE, ACCESS_DENIED_OBJECT_ACE_TYPE,
                        ACCESS_DENIED_CALLBACK_OBJECT_ACE_TYPE})
SIMPLE_ALLOW_TYPES = frozenset({ACCESS_ALLOWED_ACE_TYPE, ACCESS_ALLOWED_CALLBACK_ACE_TYPE})
INHERIT_ONLY_ACE = 0x08

OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
FOLDERID_PROGRAM_DATA = "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}"


class AnchorUncheckable(Exception):
    """The security of an anchor path could not be read."""


# ------------------------------------------------------------------ location
def program_data() -> Path:
    """%ProgramData% from the known-folder API, never from the environment
    (a process's environment is not a trust boundary)."""
    class _Guid(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]

    guid = _Guid()
    if ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(FOLDERID_PROGRAM_DATA), ctypes.byref(guid)) != 0:
        raise OSError("CLSIDFromString failed")
    buffer = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_Guid), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer)) != 0:
        raise OSError("SHGetKnownFolderPath(ProgramData) failed")
    try:
        return Path(buffer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buffer)


def anchor_file() -> Path:
    """The keyset path on this Windows computer. If ProgramData cannot be
    resolved, a path that cannot exist is returned (nothing is trusted)."""
    try:
        return program_data() / "AnyAiCam" / "trust" / KEYSET_NAME
    except OSError:
        return Path("\\\\?\\anyaicam-no-program-data\\trust") / KEYSET_NAME


# ------------------------------------------------------------------ security
def _advapi():
    advapi = ctypes.windll.advapi32
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetFileSecurityW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorOwner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.IsValidSid.argtypes = [ctypes.c_void_p]
    return advapi


def _sid_string(advapi, sid) -> str:
    if not sid or not advapi.IsValidSid(sid):
        raise AnchorUncheckable("an invalid SID")
    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise AnchorUncheckable("ConvertSidToStringSidW failed")
    try:
        return text.value
    finally:
        ctypes.windll.kernel32.LocalFree(text)


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [("ace_count", wintypes.DWORD), ("bytes_in_use", wintypes.DWORD), ("bytes_free", wintypes.DWORD)]


def security(path: Path) -> tuple[str, list[tuple[int, int, int, str]] | None]:
    """(owner SID, [(ace type, ace flags, access mask, SID), ...]) of a file
    or folder; the list is None for a NULL DACL. For an allow entry this
    module cannot parse the SID is "" and the mask is -1."""
    advapi = _advapi()
    needed = wintypes.DWORD(0)
    flags = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    advapi.GetFileSecurityW(str(path), flags, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise AnchorUncheckable(f"cannot read the security of {path} (error {ctypes.GetLastError()})")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetFileSecurityW(str(path), flags, buffer, needed, ctypes.byref(needed)):
        raise AnchorUncheckable(f"cannot read the security of {path} (error {ctypes.GetLastError()})")
    owner, defaulted = ctypes.c_void_p(), wintypes.BOOL()
    if not advapi.GetSecurityDescriptorOwner(buffer, ctypes.byref(owner), ctypes.byref(defaulted)) or not owner.value:
        raise AnchorUncheckable(f"{path} has no owner")
    owner_sid = _sid_string(advapi, owner)
    present, dacl = wintypes.BOOL(), ctypes.c_void_p()
    if not advapi.GetSecurityDescriptorDacl(buffer, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
        raise AnchorUncheckable(f"cannot read the access list of {path}")
    if not present.value or not dacl.value:
        return owner_sid, None
    info = _AclSizeInformation()
    if not advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):  # AclSizeInformation
        raise AnchorUncheckable(f"cannot read the access list of {path}")
    entries = []
    for index in range(info.ace_count):
        ace = ctypes.c_void_p()
        if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
            raise AnchorUncheckable(f"cannot read an access entry of {path}")
        ace_type = ctypes.c_ubyte.from_address(ace.value).value
        ace_flags = ctypes.c_ubyte.from_address(ace.value + 1).value
        if ace_type in SIMPLE_ALLOW_TYPES:
            mask = wintypes.DWORD.from_address(ace.value + 4).value
            entries.append((ace_type, ace_flags, mask, _sid_string(advapi, ace.value + 8)))
        elif ace_type in DENY_TYPES:
            entries.append((ace_type, ace_flags, 0, ""))
        else:
            entries.append((ace_type, ace_flags, -1, ""))  # object/conditional allow entries: unparsed
    return owner_sid, entries


def acl_problem(owner_sid: str, entries) -> str:
    """Why an owner + DACL is not a safe trust-anchor entry, or ""."""
    if owner_sid not in TRUSTED_SIDS:
        return f"is owned by {owner_sid}, not SYSTEM, Administrators or TrustedInstaller"
    if entries is None:
        return "has no access list (everyone has full access)"
    for ace_type, ace_flags, mask, sid in entries:
        if ace_type in DENY_TYPES or ace_flags & INHERIT_ONLY_ACE:
            continue
        if mask == -1:
            return f"has an access entry of type {ace_type} that cannot be checked"
        if sid in TRUSTED_SIDS or sid == OWNER_RIGHTS_SID:
            continue
        if mask & WRITE_RIGHTS:
            return f"lets {sid} change it (access mask 0x{mask:08x})"
    return ""


def entry_problem(path: Path, *, folder: bool, read_security=security) -> str:
    try:
        info = os.lstat(path)
    except OSError:
        return "is missing"
    if getattr(info, "st_file_attributes", 0) & REPARSE_POINT or stat.S_ISLNK(info.st_mode):
        return "is a symbolic link, junction or other reparse point"
    if folder and not stat.S_ISDIR(info.st_mode):
        return "is not a folder"
    if not folder and not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    try:
        owner_sid, entries = read_security(path)
    except (AnchorUncheckable, OSError) as error:
        return f"cannot be checked ({error})"
    return acl_problem(owner_sid, entries)


def anchor_problem(path: Path, *, read_security=security) -> str:
    """Why the keyset at path may not be trusted, or "". The file and its
    two parent folders (trust, AnyAiCam) are each checked."""
    for candidate, folder in ((path.parent.parent, True), (path.parent, True), (path, False)):
        problem = entry_problem(candidate, folder=folder, read_security=read_security)
        if problem:
            return f"{candidate} {problem}"
    return ""
