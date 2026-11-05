"""Where LPR field output may live: one policy for every tool that writes it.

Clips, frames and results show plates, so each destination must resolve inside
the private root, on a local fixed disk, reached through no link, under names
that can't address a stream or device. Every folder and file in the chain must
be owned by this Windows account and grant access to no one else. A folder
that fails is refused, never repaired: a repair would leave whatever it already
holds exposed for as long as it had been. Windows only; anything else refuses.

Not covered: an administrator or SYSTEM can always read through an ACL, and the
checks can't see a change made after them by someone with write access, which
this policy is there to deny.
"""
from __future__ import annotations

import ctypes
import os
import re
import stat
import sys
from ctypes import wintypes
from pathlib import Path

PRIVATE_ROOT = Path.home() / 'anyaicam-blue' / 'lpr-field-private'
SHARED_FOLDERS = ('anyaicam-1.2.7-handoff', 'onedrive', 'transfer', '.git')
SAFE_NAME = re.compile(r'^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$')
RESERVED_NAME = re.compile(r'^(con|prn|aux|nul|conin\$|conout\$|com\d|lpt\d)(\.|$)', re.IGNORECASE)
# Everyone, anonymous, authenticated users, interactive, Users, Guests.
BROAD_SIDS = {'S-1-1-0', 'S-1-5-7', 'S-1-5-11', 'S-1-5-4', 'S-1-5-32-545', 'S-1-5-32-546'}
# Write data, append, write attributes, delete child, delete, change ACL or owner, generic write or all.
WRITE_RIGHTS = 0x0002 | 0x0004 | 0x0100 | 0x0040 | 0x00010000 | 0x00040000 | 0x00080000 | 0x40000000 | 0x10000000

OWNER_SECURITY_INFORMATION, DACL_SECURITY_INFORMATION = 1, 4
SE_FILE_OBJECT = 1
SE_DACL_PROTECTED = 0x1000
ACCESS_ALLOWED_ACE_TYPE = 0
DRIVE_FIXED = 3
TOKEN_QUERY, TOKEN_USER = 0x0008, 1
SDDL_REVISION_1 = 1


class Refused(ValueError):
    """The destination breaks the policy; nothing was written."""


class _AceHeader(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('size', wintypes.WORD)]


class _Ace(ctypes.Structure):
    _fields_ = [('header', _AceHeader), ('mask', wintypes.DWORD), ('sid_start', wintypes.DWORD)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]


class _AclSize(ctypes.Structure):
    _fields_ = [('count', wintypes.DWORD), ('used', wintypes.DWORD), ('free', wintypes.DWORD)]


def _api():
    if sys.platform != 'win32':
        raise Refused('this policy runs on Windows only')
    advapi, kernel = ctypes.WinDLL('advapi32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    return advapi, kernel


def _sid_text(advapi, kernel, sid) -> str:
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def current_sid() -> str:
    """This process's own account, read from its token rather than its name."""
    advapi, kernel = _api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(256)
        size = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, TOKEN_USER, buffer, 256, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_text(advapi, kernel, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)


def read_acl(path: Path) -> dict:
    """{'owner': sid, 'protected': bool, 'entries': [(ace type, sid, mask)]}; entries None for a null DACL."""
    advapi, kernel = _api()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    failed = advapi.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                          ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if failed:
        raise ctypes.WinError(failed)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        found = {'owner': _sid_text(advapi, kernel, owner), 'protected': bool(control.value & SE_DACL_PROTECTED), 'entries': None}
        if not dacl:
            return found
        size = _AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        found['entries'] = []
        for index in range(size.count):
            pointer = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(pointer)):
                raise ctypes.WinError(ctypes.get_last_error())
            ace = ctypes.cast(pointer, ctypes.POINTER(_Ace)).contents
            sid = 'unknown'
            if ace.header.type == ACCESS_ALLOWED_ACE_TYPE or ace.header.type == 1:  # allow and deny share this layout
                sid = _sid_text(advapi, kernel, pointer.value + _Ace.sid_start.offset)
            found['entries'].append((ace.header.type, sid, ace.mask))
        return found
    finally:
        kernel.LocalFree(descriptor)


def acl_problem(path: Path, allowed: set, must_be_protected: bool) -> str | None:
    """What is wrong with this object's owner and DACL against the allowed SIDs, or None."""
    found = read_acl(path)
    if found['owner'] not in allowed:
        return f'owned by {found["owner"]}'
    if found['entries'] is None:
        return 'it has no access list, so everyone has access'
    if not found['entries']:
        return 'its access list is empty'
    for kind, sid, _mask in found['entries']:
        if kind != ACCESS_ALLOWED_ACE_TYPE or sid not in allowed:
            return f'access entry for {sid} (type {kind}) is outside the policy'
    return 'it inherits from its parent' if must_be_protected and not found['protected'] else None


def _is_link(path: Path) -> bool:
    details = os.lstat(path)
    return stat.S_ISLNK(details.st_mode) or bool(details.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _check_root(root: Path) -> Path:
    """The root must be its own real path, on a fixed disk, inside this profile,
    with nothing above it letting other people replace it."""
    _advapi, kernel = _api()
    root = Path(os.path.abspath(root))
    real = Path(os.path.realpath(root))
    if not _same(root, real):
        raise Refused(f'the private root reaches {real} through a link')
    if str(root).startswith(('\\\\', '//')) or kernel.GetDriveTypeW(root.anchor) != DRIVE_FIXED:
        raise Refused(f'{root} is not on a local fixed disk; use the private folder')
    home = Path(os.path.realpath(Path.home()))
    if not any(_same(parent, home) for parent in root.parents):
        raise Refused(f'{root} is outside this profile; use the private folder')
    for parent in root.parents:
        if not parent.exists():
            continue
        if _is_link(parent):
            raise Refused(f'{parent} is a link; use the private folder')
        found = read_acl(parent)
        for kind, sid, mask in found['entries'] or []:
            if kind == ACCESS_ALLOWED_ACE_TYPE and sid in BROAD_SIDS and mask & WRITE_RIGHTS:
                raise Refused(f'{parent} lets {sid} change it; use the private folder')
        if found['entries'] is None:
            raise Refused(f'{parent} has no access list; use the private folder')
        if _same(parent, home):
            break
    return root


def check_path(path, root: Path = None) -> Path:
    """The real absolute path, once the text and every step below the root pass.
    The path may not exist yet; steps that exist must not be links."""
    root = _check_root(root or PRIVATE_ROOT)
    text = os.fspath(path)
    if text.startswith(('\\\\', '//')):
        raise Refused(f'{text} is a network path; use the private folder')
    for part in Path(text).parts:  # abspath would quietly rewrite these, so read them as typed
        if part.rstrip('\\/') not in ('', '.', '..') and (RESERVED_NAME.match(part) or part.endswith(('.', ' '))):
            raise Refused(f'{part!r} is not an allowed name; use the private folder')
    target = Path(os.path.abspath(text))
    if not (_same(target, root) or any(_same(parent, root) for parent in target.parents)):
        raise Refused(f'{text} is outside {root}; use the private folder')
    if any(name in part.lower() for part in target.parts for name in SHARED_FOLDERS):
        raise Refused(f'{text} may be shared; use the private folder')
    step = root
    if step.exists() and _is_link(step):
        raise Refused(f'{step} is a link; use the private folder')
    for part in target.parts[len(root.parts):]:
        if not SAFE_NAME.match(part) or RESERVED_NAME.match(part) or part.endswith('.'):
            raise Refused(f'{part!r} is not an allowed name; use the private folder')
        step = step / part
        if os.path.lexists(step) and _is_link(step):
            raise Refused(f'{step} is a link; use the private folder')
    real = Path(os.path.realpath(target))
    if not _same(real, target):  # e.g. an 8.3 short name or a link the walk could not see
        raise Refused(f'{text} really is {real}; use the private folder')
    return target


def _make_folder(folder: Path, sid: str) -> None:
    """Created locked: there is no moment when the folder holds an inherited ACL."""
    advapi, kernel = _api()
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(f'O:{sid}D:P(A;OICI;FA;;;{sid})', SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(folder), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def check_tree(folder: Path, allowed: set) -> None:
    """Every folder and file already inside, without following anything."""
    problem = acl_problem(folder, allowed, must_be_protected=True)
    if problem:
        raise Refused(f'{folder}: {problem}; it is not repaired, so move it away and start a new folder')
    with os.scandir(folder) as entries:
        for entry in entries:
            path = Path(entry.path)
            if _is_link(path):
                raise Refused(f'{path} is a link; use the private folder')
            if entry.is_dir(follow_symlinks=False):
                check_tree(path, allowed)
                continue
            check_file(path, allowed)


def check_file(path: Path, allowed: set) -> None:
    if os.lstat(path).st_nlink != 1:
        raise Refused(f'{path} has another hard link; use the private folder')
    problem = acl_problem(path, allowed, must_be_protected=False)
    if problem:
        raise Refused(f'{path}: {problem}')


def prepare_folder(folder, root: Path = None) -> Path:
    """The checked folder, made locked where missing and verified, contents too, where not."""
    root = root or PRIVATE_ROOT
    target = check_path(folder, root)
    resolved_root = Path(os.path.abspath(root))
    sid = current_sid()
    allowed = {sid}
    step = resolved_root
    for part in (None, *target.parts[len(resolved_root.parts):]):
        step = step if part is None else step / part
        if not step.exists():
            if part is None:
                step.parent.mkdir(parents=True, exist_ok=True)
            _make_folder(step, sid)
    check_path(target, root)
    check_tree(resolved_root, allowed)
    return target


def open_new(path, root: Path = None, mode: str = 'x', **options):
    """A new file in a checked private folder, checked before any plate data is
    written. 'x' never reuses a name, so nothing kept is replaced."""
    if 'x' not in mode:
        raise Refused('private files are only ever created new')
    target = check_path(path, root)
    prepare_folder(target.parent, root)
    handle = open(target, mode, **options)
    try:
        check_file(target, {current_sid()})
    except Exception:
        handle.close()
        target.unlink(missing_ok=True)
        raise
    return handle


def seal(folder, root: Path = None) -> None:
    """Re-checks a finished folder: a file written by another library, like an
    image encoder, is only proven private here."""
    target = check_path(folder, root)
    check_tree(target, {current_sid()})
