"""My orders in the customer VMS navigation (Green 2026-10-17).

/customer/orders (hardware and relay orders, carrier and tracking) had no navigation entry: it was reached only from
the relay panel while equipment was on its way and from order emails, so once an order was delivered a customer could
not find it (or its tracking) again. It is now a sidebar item right after My subscription for customer owners and
viewers (the same people the page itself admits), and hidden from administrators, who have no customer orders.
"""
import re

import pytest

import partner_portal
from test_aaco_floating_widget import _admin_cookie, _owner_cookie, _viewer_cookie, client, db_path  # noqa: F401 -- fixtures

SIDEBAR = re.compile(r'<nav class="nav" aria-label="Primary">(.*?)</nav>', re.S)


def _get(client, path, cookie):
    return client.get(path, cookies={partner_portal.SESSION_COOKIE: cookie})


def _sidebar_hrefs(html):
    match = SIDEBAR.search(html)
    assert match, "navigation not found"
    return re.findall(r'href="([^"]+)"', match.group(1))


@pytest.mark.parametrize("cookie", [_owner_cookie, _viewer_cookie])
def test_my_orders_is_in_the_customer_sidebar_right_after_my_subscription(client, cookie):
    html = _get(client, "/dashboard", cookie()).text
    hrefs = _sidebar_hrefs(html)
    assert hrefs.count("/customer/orders") == 1
    assert hrefs.index("/subscription-portal") + 1 == hrefs.index("/customer/orders")
    assert re.search(r'<a [^>]*href="/customer/orders"[^>]*>.*?My orders', SIDEBAR.search(html).group(1), re.S)


@pytest.mark.parametrize("cookie", [_owner_cookie, _viewer_cookie])
def test_the_my_orders_entry_opens_the_orders_page(client, cookie):
    response = _get(client, "/customer/orders", cookie())
    assert response.status_code == 200
    assert "My orders" in response.text


def test_administrators_get_no_my_orders_link(client):
    response = _get(client, "/dashboard", _admin_cookie())
    assert 'href="/customer/orders"' not in response.text
