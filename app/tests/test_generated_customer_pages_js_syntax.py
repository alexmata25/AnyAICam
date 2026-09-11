"""Regression coverage for a real, confirmed-live production incident:
the dedicated single-camera Live View page (/customer/cameras/{id}/live)
shipped with a malformed JavaScript template literal --

    ${item.enabled?'':' <span class='pill wait' style='margin-left:4px'>Upgrade</span>'}

-- unescaped single quotes inside a single-quote-delimited JS string,
nested inside a template literal, produced a real browser-side
`Uncaught SyntaxError: Missing } in template expression`. Because the
broken code lived inside the SAME <script> block as every other handler
on the page (session start, HLS playback, polling), the syntax error
silently killed the entire script -- the page rendered a normal-looking
placeholder, gave no error to the customer, and the video simply never
started. No prior test caught this: existing coverage checked HTML
content/authorization only, never that the embedded JavaScript was
actually valid syntax.

This suite extracts every inline <script>...</script> block from the
generated HTML of the customer-facing Live View pages and parses each
one with a real JS engine (esprima) -- this is the general protection
the incident calls for: any future malformed template/string in these
pages fails a test before it ever reaches a browser, regardless of which
exact expression is broken next time.

Covers both /customer-live (the multi-camera grid, which happened to
have no such bug) and /customer/cameras/{camera_id}/live (the dedicated
page, which did) -- and both an 'online' and a 'degraded' appliance
health state, since the two pages' status pre-checks/behavior differ (see
test_customer_camera_status_health_states.py for that fix's own
dedicated coverage) and a syntax error could in principle hide behind
either code path.
"""

import re

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from cloud_config import settings
from database_backend import override_target

try:
    import esprima
except ImportError:  # pragma: no cover - environment without the test-only JS parser
    esprima = None

pytestmark = pytest.mark.skipif(esprima is None, reason="esprima not installed -- see requirements-test.txt")

_SCRIPT_BLOCK = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def _extract_inline_scripts(html: str) -> list[str]:
    """Every <script> block WITHOUT a src= attribute -- i.e. every block
    whose JavaScript text this codebase actually generated and could
    have broken, as opposed to a <script src="..."> reference to an
    external file (hls.js from a CDN) with no embedded text to check."""
    return [match.group(1) for match in _SCRIPT_BLOCK.finditer(html) if match.group(1).strip()]


def _assert_all_scripts_parse(html: str, page_label: str) -> None:
    scripts = _extract_inline_scripts(html)
    assert scripts, f"{page_label}: expected at least one inline <script> block, found none"
    for index, script in enumerate(scripts):
        try:
            esprima.parseScript(script)
        except Exception as error:  # esprima.Error and subclasses
            raise AssertionError(
                f"{page_label}: inline <script> block #{index} is not valid JavaScript -- "
                f"a browser would fail with a SyntaxError and the entire block (including "
                f"unrelated handlers in the same tag) would silently stop working.\n"
                f"Parser error: {error}\n"
                f"Script (first 2000 chars):\n{script[:2000]}"
            ) from error


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_generated_pages_js_syntax.db"


def _seed(conn, online_status):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,online_status,created_at) "
        "VALUES('appl-a','cust-a','site-a','AIC-A',?,'2026-01-01')",
        (online_status,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'Customer A Camera','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)


def _get_page(db_path, online_status, path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed(conn, online_status)
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as client:
            client.cookies.set("anyaicam_partner_session", _owner_cookie())
            return client.get(path)


@pytest.mark.parametrize("online_status", ["online", "degraded"])
def test_multi_camera_grid_page_scripts_are_valid_js(db_path, online_status):
    response = _get_page(db_path, online_status, "/customer-live")
    assert response.status_code == 200, response.text
    _assert_all_scripts_parse(response.text, f"/customer-live (online_status={online_status})")


@pytest.mark.parametrize("online_status", ["online", "degraded"])
def test_dedicated_camera_page_scripts_are_valid_js(db_path, online_status):
    response = _get_page(db_path, online_status, "/customer/cameras/cam-a/live")
    assert response.status_code == 200, response.text
    _assert_all_scripts_parse(response.text, f"/customer/cameras/cam-a/live (online_status={online_status})")
