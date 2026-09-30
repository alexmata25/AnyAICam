"""Root service worker freshness (2026-09-30).

Customers had to press Ctrl+Shift+R -- which bypasses a service worker -- to
get thumbnails and live video working after a release. The worker served every
/static/ file cache-first from a cache whose name never changed, and routed
every same-origin request (API, thumbnails, live playlists, clips) through
itself. These tests execute the shipped worker's fetch handler under Node.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _worker(monkeypatch, build="e690f109c187abcd"):
    monkeypatch.setenv("ANYAICAM_BUILD_ID", build)
    from fastapi import FastAPI
    import pwa_routes
    app = FastAPI()
    pwa_routes.register_pwa_routes(app, page_shell=lambda *a, **k: "", current_user=lambda request: None)
    response = TestClient(app).get("/service-worker.js")
    assert response.status_code == 200
    return response


def test_cache_name_follows_the_build_and_the_script_is_rechecked(monkeypatch):
    response = _worker(monkeypatch)
    assert "const CACHE='anyaicam-static-e690f109c187';" in response.text
    assert "anyaicam-mobile-v1" not in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert _worker(monkeypatch, "c0e7e049a076").text != response.text  # a new build installs a new worker


HARNESS = r"""
const fs = require('fs');
const listeners = {};
const cacheStore = new Map();
const cache = { put: async (req, res) => cacheStore.set(req.url, res), addAll: async () => {} };
global.caches = { open: async () => cache, keys: async () => [], delete: async () => true,
  match: async (req) => cacheStore.get(typeof req === 'string' ? req : req.url) };
global.self = { addEventListener: (n, fn) => { listeners[n] = fn; }, skipWaiting() {}, clients: { claim() {} }, registration: {} };
global.location = { origin: 'https://portal.example' };
global.clients = {};
let network = 'ok';
global.fetch = async (req) => { if (network !== 'ok') throw new TypeError('offline');
  return { ok: true, url: req.url, from: 'network', clone() { return { ok: true, url: req.url, from: 'cache' }; } }; };
global.Response = { error: () => ({ from: 'error' }) };
eval(fs.readFileSync(process.argv[2], 'utf8'));
function request(path, mode) { return { method: 'GET', url: 'https://portal.example' + path, mode: mode || 'cors' }; }
async function handle(path, mode) {
  let responded = null;
  listeners.fetch({ request: request(path, mode), respondWith: (p) => { responded = p; } });
  return responded ? await responded : 'not-intercepted';
}
(async () => {
  const out = {};
  for (const path of ['/api/customer/events/e1/thumbnail', '/api/customer/cameras/c1/live/playlist.m3u8',
                      '/static/hls/camera1.m3u8', '/api/customer/events/e1/media/url']) {
    out[path] = await handle(path);
  }
  out.static_online = (await handle('/static/event_media.js?v=1')).from;
  await new Promise(r => setTimeout(r, 0));
  network = 'down';
  out.static_offline = (await handle('/static/event_media.js?v=1')).from;
  out.navigate_offline = await handle('/offline', 'navigate');
  console.log(JSON.stringify(out));
})();
"""


def test_only_static_files_are_intercepted_network_first(monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    source = _worker(monkeypatch).text
    with tempfile.TemporaryDirectory() as tmp:
        worker = Path(tmp) / "sw.js"
        worker.write_text(source, encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        done = subprocess.run([node, str(harness), str(worker)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    for path in ('/api/customer/events/e1/thumbnail', '/api/customer/cameras/c1/live/playlist.m3u8',
                 '/static/hls/camera1.m3u8', '/api/customer/events/e1/media/url'):
        assert out[path] == "not-intercepted", path
    assert out["static_online"] == "network"   # fresh from the network whenever online
    assert out["static_offline"] == "cache"    # the cached copy is only an offline fallback
