"""Facial Recognition / People on the phone (2026-10-01).

It was reachable only from the desktop sidebar: the phone's bottom bar and
the Account hub had no entry. Now an account with the Face Access add-on
gets "People" in the phone bar, and every customer sees Facial Recognition
on the Account hub (which explains the add-on when it is missing).
"""
import sqlite3

import partner_portal
from test_notification_settings import _owner_cookie, _seed_owner, _seed_tenant, db_path, http_client  # noqa: F401


def _bottom_bar(html: str) -> str:
    start = html.index('class="mobile-nav')
    return html[start:html.index("</nav>", start)]


def _seed(db_path, *, face_access: bool):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn)
    _seed_owner(conn, "user-1", "owner@example.test", "cust-1")
    if face_access:
        conn.execute("INSERT INTO analytics_subscriptions(id,customer_id,site_id,analytic_key,status,licensed_quantity,created_at,updated_at) "
                     "VALUES('fa-1','cust-1',NULL,'facial_recognition','active',1,'2026-10-01','2026-10-01')")
    conn.commit()
    conn.close()


def test_face_access_account_gets_people_in_the_phone_bar(http_client, db_path):
    _seed(db_path, face_access=True)
    html = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert 'href="/aac/people">People</a>' in _bottom_bar(html)


def test_without_face_access_the_phone_bar_stays_as_it_was(http_client, db_path):
    _seed(db_path, face_access=False)
    html = http_client.get("/dashboard", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert "/aac/people" not in _bottom_bar(html)


def test_the_account_hub_links_facial_recognition(http_client, db_path):
    _seed(db_path, face_access=False)
    html = http_client.get("/customer-portal", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text
    assert 'href="/aac/people"><div><strong>Facial Recognition</strong>' in html
