"""Fail-closed preflight for the isolated Local Gateway HTTP suite.

Default: preflight only. --run remains blocked until independent OS-enforced
filesystem and network isolation verifiers are implemented and pass.
No production configuration, network service or database is modified here.
"""
from __future__ import annotations
import argparse
import ast
import ctypes
from ctypes import wintypes
import importlib.util
import importlib.metadata
import os
from pathlib import Path
import shutil
import sys
import tempfile
import winreg

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "app" / "tests" / "local_gateway_http_harness.py"
GUARDS = ROOT / "app" / "tests" / "gateway_http_guards.py"
TEST_MODULE = "app.tests.test_local_gateway_http_acceptance"
GIB = 1024 ** 3


def _win32():
    if os.name != "nt":
        raise RuntimeError("Windows is required")
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    a.OpenProcessToken.restype = wintypes.BOOL
    a.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    a.OpenThreadToken.restype = wintypes.BOOL
    a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    a.GetTokenInformation.restype = wintypes.BOOL
    a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    a.ConvertSidToStringSidW.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetCurrentThread.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.LocalFree.argtypes = [ctypes.c_void_p]
    return a, k


def _token_info(a, handle, kind):
    needed = wintypes.DWORD()
    a.GetTokenInformation(handle, kind, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buf = ctypes.create_string_buffer(needed.value)
    if not a.GetTokenInformation(handle, kind, buf, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf


def _sid_string(a, k, sid):
    if not sid:
        raise RuntimeError("missing SID")
    result = wintypes.LPWSTR()
    if not a.ConvertSidToStringSidW(sid, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return result.value
    finally:
        k.LocalFree(ctypes.cast(result, ctypes.c_void_p))


def token_details():
    a, k = _win32()
    thread_token = wintypes.HANDLE()
    if a.OpenThreadToken(k.GetCurrentThread(), 0x0008, True, ctypes.byref(thread_token)):
        k.CloseHandle(thread_token)
        raise RuntimeError("thread impersonation is active; effective identity is ambiguous")
    if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN
        raise ctypes.WinError(ctypes.get_last_error())
    h = wintypes.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(h)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        user = _token_info(a, h, 1)  # TOKEN_USER; first member is SID pointer
        user_sid = _sid_string(a, k, ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0])
        is_ac = ctypes.cast(_token_info(a, h, 29), ctypes.POINTER(wintypes.DWORD))[0]
        caps = ctypes.cast(_token_info(a, h, 30), ctypes.POINTER(wintypes.DWORD))[0]
        app_sid = None
        if is_ac:
            app = _token_info(a, h, 31)
            app_sid = _sid_string(a, k, ctypes.cast(app, ctypes.POINTER(ctypes.c_void_p))[0])
        return user_sid, bool(is_ac), caps, app_sid
    finally:
        k.CloseHandle(h)


def hkcu_kernel_name():
    """Resolve HKCU's actual registry hive name, not merely its display alias."""
    n = ctypes.WinDLL("ntdll")
    n.NtQueryKey.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    n.NtQueryKey.restype = ctypes.c_long
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "", 0, winreg.KEY_READ) as key:
        needed = wintypes.ULONG()
        n.NtQueryKey(wintypes.HANDLE(int(key)), 3, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > 65536:
            raise RuntimeError("cannot resolve HKCU hive")
        buf = ctypes.create_string_buffer(needed.value)
        status = n.NtQueryKey(wintypes.HANDLE(int(key)), 3, buf, needed.value, ctypes.byref(needed))
        if status != 0:
            raise RuntimeError("NtQueryKey failed: 0x%08x" % (status & 0xffffffff))
        size = ctypes.cast(buf, ctypes.POINTER(wintypes.ULONG))[0]
        return ctypes.wstring_at(ctypes.addressof(buf) + 4, size // 2)


def profile_check(user_sid):
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" + "\\" + user_sid
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
        configured = os.path.expandvars(winreg.QueryValueEx(key, "ProfileImagePath")[0])
    actual = os.environ.get("USERPROFILE", "")
    if not actual or os.path.normcase(os.path.realpath(actual)) != os.path.normcase(os.path.realpath(configured)):
        raise RuntimeError("USERPROFILE does not match the token user's ProfileList")
    with winreg.OpenKey(winreg.HKEY_USERS, user_sid, 0, winreg.KEY_READ):
        pass
    expected_hive = "\\registry\\user\\" + user_sid.lower()
    if hkcu_kernel_name().lower() != expected_hive:
        raise RuntimeError("HKCU is not the token user's loaded hive")
    return "SID, ProfileList, USERPROFILE and HKCU agree"


def resource_check():
    class Memory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("page", ctypes.c_ulonglong), ("page_free", ctypes.c_ulonglong),
                    ("virtual", ctypes.c_ulonglong), ("virtual_free", ctypes.c_ulonglong),
                    ("extended", ctypes.c_ulonglong)]
    m = Memory(); m.length = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise ctypes.WinError()
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if m.available < 4 * GIB or free < GIB:
        raise RuntimeError("requires at least 4 GiB available RAM and 1 GiB free temp disk")
    return "%.2f GiB available RAM; %.2f GiB free temp disk" % (m.available / GIB, free / GIB)


def _ordered(source, markers):
    position = -1
    for marker in markers:
        position = source.find(marker, position + 1)
        if position < 0:
            raise RuntimeError("fixture guard missing or out of order: " + marker)


def database_check():
    temp = Path(tempfile.gettempdir()).resolve(strict=True)
    if not temp.is_dir():
        raise RuntimeError("temporary root is unavailable")
    if temp == ROOT or ROOT in temp.parents:
        raise RuntimeError("temporary root overlaps the application checkout")
    source = FIXTURE.read_text(encoding="utf-8")
    ast.parse(source)
    guards = GUARDS.read_text(encoding="utf-8")
    ast.parse(guards)
    _ordered(source, ['TemporaryDirectory(prefix="aac-gateway-http-")', 'stack.enter_context(sqlite_audit_scope())',
                      'make_sqlite_connection_class(db_path, original_class)',
                      'make_sqlite_connect_guard(db_path, original_connect, confined_class)',
                      'for module in (sqlite3, _sqlite3):',
                      'patch.object(module, "connect", confined_connect)',
                      'patch.object(module, "Connection", confined_class)',
                      'db = sqlite3.connect(db_path)', 'app = app_factory(fixtures)',
                      'override_target(sqlite_path=db_path)', 'import partner_db',
                      'partner_db.initialize_database()'])
    for marker in ('validate_sqlite_target(database, approved_file)', 'factory=connection_class', 'sys.addaudithook(_sqlite_audit)',
                   'sqlite3.SQLITE_ATTACH', 'def set_authorizer(', 'def enable_load_extension('):
        if marker not in guards:
            raise RuntimeError("SQLite confinement guard missing: " + marker)
    return "source declares one disposable target and installs SQLite guards before app import; runtime isolation unverified"


def stripe_check():
    source = FIXTURE.read_text(encoding="utf-8")
    ast.parse(source)
    body = source[source.index("def full_vms_app("):]
    _ordered(body, ['import stripe_state', 'patch.object(stripe_state, "stripe_get"',
                    'patch.object(stripe_state, "current_subscription"', 'import checkout_guard',
                    'patch.object(checkout_guard, "canonical_stripe_customer"',
                    'patch.object(checkout_guard, "create_session"', 'import per_camera_billing',
                    'patch.object(per_camera_billing, "own_pc_licence_reversed"',
                    'partner_db.initialize_database()', '    import main\n'])
    return "source installs synthetic Stripe state and checkout mocks before main import; other provider paths remain unverified"


def checkout_check():
    if os.environ.get("ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED", "").strip().lower() == "true":
        raise RuntimeError("checkout is already enabled in the launching process")
    source = FIXTURE.read_text(encoding="utf-8")
    if 'patch.dict(os.environ, os_base, clear=True)' not in source or '"ANYAICAM_LOCAL_GATEWAY_CHECKOUT_ENABLED": "true"' not in source:
        raise RuntimeError("test-only checkout override not found in isolated fixture")
    return "parent checkout flag is off; fixture declares a process-local override"


def dependencies_check():
    required = {"fastapi": "0.115.6", "httpx": "0.28.1", "starlette": "0.41.3",
                "cryptography": "44.0.0"}
    problems = []
    for name, expected in required.items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = "missing"
        if installed != expected:
            problems.append("%s=%s (expected %s)" % (name, installed, expected))
    if problems:
        raise RuntimeError("; ".join(problems))
    return "direct dependency versions match the existing pins; full VMS imports remain unverified"


def isolation_check(details):
    _, in_appcontainer, caps, sid = details
    if not in_appcontainer or not sid:
        raise RuntimeError("the current process is not an AppContainer")
    if caps:
        raise RuntimeError("AppContainer has capabilities; zero are required")
    # An empty capability list does not prove the host's actual WFP policy,
    # explicit allows, loopback exemptions, or controlled-canary denial.
    # Do not accept an environment flag or an operator assertion as proof.
    raise RuntimeError("independent OS-enforced outbound-denial verifier is not implemented")


def confinement_check():
    # A captured sqlite3.Connection.set_authorizer descriptor can remove the
    # authorizer; native code and pre-existing handles also bypass Python guards.
    # A future verifier must independently establish OS-enforced filesystem
    # isolation for the effective test process, including denial of other data
    # and inherited handles. An environment flag or operator assertion is not
    # evidence of isolation. No such verifier exists yet.
    raise RuntimeError("independent OS-enforced filesystem isolation verifier is not implemented")


def test_credentials_check(environ=None):
    """Reject inherited provider secrets; never treat a clean environment as proof."""
    source = os.environ if environ is None else environ
    for key, value in source.items():
        name = key.upper()
        if value and (name.startswith(('AWS_', 'PG', 'STRIPE_')) or
                      (name.startswith('ANYAICAM_') and any(part in name for part in
                       ('STRIPE', 'DATABASE_URL', 'PARTNER_DB', 'SECRET', 'TOKEN')))):
            raise RuntimeError('inherited provider or database configuration is present')
    # An empty environment cannot establish inherited-handle, filesystem,
    # service credential or machine-profile isolation.
    raise RuntimeError('independent test-only credential isolation verifier is not implemented')


def database_isolation_check():
    # Source-level SQLite guard checks do not prove that inherited/native
    # connections or external services cannot reach a production database.
    raise RuntimeError('independent OS-enforced database isolation verifier is not implemented')


def _literal_false(path, name):
    """Inspect a source gate without importing the application."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or values[0].value is not False:
        raise RuntimeError(name + " is not a single literal False")
    return "source gate is disabled"


def integration_plan_preflight():
    """Read-only preparation only: no database, application or provider calls."""
    print("READ-ONLY Gateway integration preparation; no target is contacted.")
    checks = (
        ("paid activation", ROOT / "app" / "gateway_checkout_attempts.py", "PAID_ACTIVATION_READY"),
        ("attempt service", ROOT / "app" / "gateway_checkout_attempts.py", "ATTEMPTS_SERVICE_READY"),
        ("stable Claim ID", ROOT / "app" / "gateway_stable_claim_id.py", "STABLE_CLAIM_ID_READY"),
        ("Local checkout", ROOT / "app" / "local_gateway_remote_billing.py", "CHECKOUT_AUTHORIZATION_ATOMICALLY_ENFORCED"),
        ("Cloud checkout", ROOT / "app" / "cloud_gateway_checkout.py", "CHECKOUT_HANDOFF_VERIFIED"),
    )
    for label, path, name in checks:
        try:
            print("PASS", label + ":", _literal_false(path, name))
        except Exception as error:
            print("FAIL", label + ":", str(error))
    try:
        tree = ast.parse((ROOT / "app" / "db_migrations.py").read_text(encoding="utf-8"))
        versions = {node.elts[0].value:node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant)
                    and isinstance(node.elts[0].value, str)}
        required = ("20260910_appliance_claims", "20261106_one_current_completed_claim_per_appliance",
                    "20261107_one_current_claim_session_per_device", "20261103_appliance_gateway_edition",
                    "20261104_appliance_edition_provisioning_decisions", "20261105_internal_gateway_provisioning_authorizations",
                    "20261108_cloud_gateway_camera_entitlements", "20261109_cloud_gateway_subscription_evidence",
                    "20261110_gateway_checkout_attempts", "20261111_gateway_stable_installation_claim_ids",
                    "20261103_local_gateway_remote_subscriptions")
        if any(version not in versions for version in required) or any(versions[a] >= versions[b] for a,b in zip(required,required[1:])):
            raise RuntimeError("Gateway migration source is missing or out of order")
        print("PASS migration source: required dependencies appear in source order; none executed")
    except Exception as error:
        print("FAIL migration source:", str(error))
    for label, function in (("HTTP database fixture source", database_check), ("Stripe mock source", stripe_check), ("resources", resource_check)):
        try: print("PASS", label + ":", function())
        except Exception as error: print("FAIL", label + ":", str(error))
    configured=bool(os.environ.get("ANYAICAM_GATEWAY_ACCEPTANCE_POSTGRES_URL", "").strip())
    print("STOP PostgreSQL: dedicated disposable target", "declared but not connected or independently verified" if configured else "not declared; an isolated disposable target is required")
    key=os.environ.get("ANYAICAM_STRIPE_SECRET_KEY", "").strip()
    mode="test-shaped key present; account and Prices unverified" if key.startswith(("sk_test_","rk_test_")) else ("live-shaped key present; do not use for this test" if key.startswith(("sk_live_","rk_live_")) else "test access not established")
    print("STOP Stripe:", mode)
    print("STOP isolation: independent filesystem, database and network verification is required; AWS uploads remain held.")
    print("STOP: plan only. No tests, migrations, provider requests or application startup were performed.")
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run only after every preflight passes")
    parser.add_argument("--integration-plan", action="store_true", help="read-only Gateway integration readiness; never connects or runs tests")
    args = parser.parse_args()
    if args.integration_plan:
        if args.run: parser.error("--integration-plan cannot be combined with --run")
        return integration_plan_preflight()
    failures = []
    details = None
    def check(name, fn):
        try:
            result = fn()
            print("PASS", name + ":", result, flush=True)
            return result
        except Exception as exc:
            print("FAIL", name + ":", str(exc), flush=True)
            failures.append(name)
            return None
    details = check("process token", token_details)
    check("profile and HKCU", lambda: profile_check(details[0]) if details else (_ for _ in ()).throw(RuntimeError("token unavailable")))
    check("resources", resource_check)
    check("disposable database target", database_check)
    check("checkout override", checkout_check)
    check("Stripe mocks", stripe_check)
    check("dependencies", dependencies_check)
    check("OS-enforced outbound denial", lambda: isolation_check(details) if details else (_ for _ in ()).throw(RuntimeError("token unavailable")))
    check("OS-enforced filesystem isolation", confinement_check)
    check("OS-enforced database isolation", database_isolation_check)
    check("test-only credential isolation", test_credentials_check)
    if failures:
        print("STOP: no migrations or HTTP tests were started; failed: " + ", ".join(failures), flush=True)
        return 2
    if not args.run:
        print("Preflight passed. No tests requested.")
        return 0
    # Both flags are set only after every check has passed, in this process.
    os.environ["ANYAICAM_APPROVED_HTTP_ACCEPTANCE"] = "1"
    os.environ["ANYAICAM_APPROVED_DISPOSABLE_MIGRATIONS"] = "1"
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName(TEST_MODULE)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
