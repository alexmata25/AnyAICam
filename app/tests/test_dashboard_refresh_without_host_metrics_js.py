"""Cloud customer Dashboard refresh (2026-10-01): seen live on staging
cc8043c, signed in as a real customer. 25588f5 stopped a cloud customer's
page from fetching the host's /api/system/metrics (metrics is null there)
but only guarded the CPU/memory writes; the three storage writes still read
metrics.storage_*. The TypeError dropped updateDashboard() into its catch:
Today's activity stayed "—", AI activity summary and Smart alerts stayed
"Loading…", and Camera health said "Status service unavailable", although
/api/customer/dashboard/intelligence answered 200 with the customer's data.

Runs the real, rendered updateDashboard() in node against a stub DOM, on the
cloud (no host metrics) and on the edge appliance (host metrics polled).
Same node policy as test_p05_mobile_poll_js.py: a missing node skips
locally, and fails when ANYAICAM_REQUIRE_JS_TESTS=true.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from test_dashboard_cloud_appliance_health import (  # noqa: F401 -- fixtures
    _dashboard,
    _seed,
    cloud,
    db_path,
    http_client,
)

_HARNESS = r"""
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {id, textContent: '', hidden: false, style: {}, dataset: {},
      classList: {add() {}, remove() {}, toggle() {}}};
  }
  return elements[id];
}
const CONFIG = %CONFIG%;
if (CONFIG.hostMetrics) el('cpu-metric').dataset.live = '1';
globalThis.window = {__anyaicamCustomerDashboard: !CONFIG.hostMetrics};
globalThis.document = {getElementById: el, querySelectorAll: () => []};
const fetched = [];
globalThis.fetch = async (url) => {
  fetched.push(String(url).split('?')[0]);
  const body = String(url).startsWith('/api/cameras/status')
    ? {cameras: [{camera: 1, online: true, stream: 'online', recording: 'running'}]}
    : String(url).startsWith('/api/system/metrics')
      ? {cpu_percent: 12, memory_percent: 34, storage_free_gb: 56, storage_percent: 78}
      : {events_today: 802};
  return {json: async () => body};
};
let rendered = null;
globalThis.renderIntelligence = (data) => { rendered = data; };
%FUNCTION%
await updateDashboard();
console.log(JSON.stringify({
  rendered,
  fetched,
  summary: el('dashboard-camera-summary').textContent,
  storage: el('storage-metric').textContent,
}));
"""


def _update_dashboard_source(html: str) -> str:
    """dashboardIntelligenceUrl() and updateDashboard(), exactly as rendered."""
    start = html.index("function dashboardIntelligenceUrl(){")
    depth = 0
    body = html.index("async function updateDashboard(){", start)
    for i in range(html.index("{", body), len(html)):
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        if depth == 0:
            return html[start:i + 1]
    raise AssertionError("updateDashboard() is not closed in the rendered Dashboard")


def _run(html: str, host_metrics: bool) -> dict:
    node = shutil.which("node")
    if not node:
        if os.environ.get("ANYAICAM_REQUIRE_JS_TESTS", "").lower() == "true":
            pytest.fail("node is required (ANYAICAM_REQUIRE_JS_TESTS=true) but not installed")
        pytest.skip("node is not installed in this environment")
    script = (_HARNESS
              .replace("%CONFIG%", json.dumps({"hostMetrics": host_metrics}))
              .replace("%FUNCTION%", _update_dashboard_source(html)))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "dashboard_refresh.test.mjs")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        result = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_cloud_customer_dashboard_renders_its_data_without_host_metrics(http_client, db_path, cloud):
    _seed(db_path, "cust-one", [("online", 37.4, 52.6, 457.0, 284.0)])
    html = _dashboard(http_client, "cust-one")
    outcome = _run(html, host_metrics=False)
    assert "/api/system/metrics" not in outcome["fetched"]
    assert outcome["summary"] == "1 of 1 cameras online"  # not "Status service unavailable"
    assert outcome["rendered"] == {"events_today": 802}  # Today's activity, AI summary and alerts are painted
    assert outcome["storage"] == ""  # the server-rendered appliance storage is left alone


def test_edge_dashboard_still_paints_host_metrics(http_client, db_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "RUNTIME_ROLE", "edge")
    _seed(db_path, "cust-edge")
    html = _dashboard(http_client, "cust-edge")
    outcome = _run(html, host_metrics=True)
    assert "/api/system/metrics" in outcome["fetched"]
    assert outcome["summary"] == "1 of 1 cameras online"
    assert outcome["rendered"] == {"events_today": 802}
    assert outcome["storage"] == "56 GB"
