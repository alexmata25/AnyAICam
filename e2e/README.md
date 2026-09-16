# AnyAiCam e2e (Playwright) browser tests

Real-browser tests that operate the AnyAiCam customer VMS portal the way
a human would -- not `app/tests/`'s pytest-level unit/integration suite
(no `import main`, no direct DB access, no FastAPI `TestClient`). Targets
**staging by default**; see `docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md`
for the full permission model this directory operates under, and why
production is hard-blocked unless explicitly opted into.

Established 2026-09-15. Foundation only so far: one real, passing test
(`tests/test_00_framework_smoke.py`) proves the harness can launch
staging and reach the real login screen. Every other file in `tests/` is
a scaffolded, real-route-verified target (see each file's own
docstring for exactly what's confirmed vs. still TODO) that starts
running the moment a dedicated staging test-tenant login exists --
nothing needs to be un-skipped by hand, since the skip comes from the
`e2e_credentials` fixture, not a hard-coded marker.

## One-time setup (already done on this Dell, documented for any other machine)

```bash
pip install -r e2e/requirements.txt
python -m playwright install chromium
```

## Providing credentials (do this locally -- never in chat, never in source)

```bash
cp e2e/.env.example e2e/.env
```

Then edit `e2e/.env` **on this machine, in a text editor** and fill in:

- `ANYAICAM_E2E_USERNAME` / `ANYAICAM_E2E_PASSWORD` — a **dedicated
  staging test-tenant** login, not the real pilot customer's own
  credentials (see `docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md`).

`e2e/.env` is already `.gitignore`d (both by name and by the repo's
existing bare `.env` rule) -- `git status` should never show it as a
change to commit. If it ever does, stop and check `.gitignore` before
committing anything.

## Running

```bash
# From the repo root:
cd e2e
python -m pytest -v \
  --screenshot=only-on-failure \
  --video=retain-on-failure \
  --tracing=retain-on-failure \
  --output=artifacts
```

`--screenshot`/`--video`/`--tracing` are pytest-playwright's own built-in
capture flags (no custom code needed) -- everything lands under
`e2e/artifacts/` (already `.gitignore`d). Console-message and
failed-network-request capture is this project's own addition
(`conftest.py`'s `console_and_network_capture` fixture, autouse on every
test) -- written to `e2e/artifacts/console_network/<test-name>.json` on
any test failure.

Run a single target area:

```bash
python -m pytest tests/test_playback.py -v
```

## Safety rails already built in

- **Production is hard-blocked.** `conftest.py` raises a `pytest.UsageError`
  at session startup (not a soft skip) if `ANYAICAM_E2E_BASE_URL` contains
  the real production domain, unless `ANYAICAM_E2E_ALLOW_PRODUCTION=true`
  is also explicitly set -- and even then, this directory's tests only
  ever *read* pages; nothing here performs a mutating action against
  production regardless of that flag.
- **No credentials anywhere in this directory's own code.** Every
  credential comes from `e2e/.env` (git-ignored) or a real environment
  variable at runtime -- `git grep -i password e2e/` should only ever
  turn up this README, `.env.example`'s own comments, and
  `conftest.py`'s variable *names*.
- Defaults to **staging** (`https://portal-staging.anyaicam.com`) if
  `ANYAICAM_E2E_BASE_URL` is unset at all.

## Current status

| Area | File | Status |
|---|---|---|
| Framework proof-of-life | `test_00_framework_smoke.py` | ✅ Passing today, no credentials needed |
| Authentication | `test_authentication.py` | Scaffolded, needs test-tenant login |
| Dashboard/camera visibility | `test_dashboard.py` | Scaffolded, needs test-tenant login |
| Live View | `test_live_view.py` | Scaffolded, needs test-tenant login |
| Playback (+ camera/date/time selection, 24/7 coverage, motion/event playback, thumbnails) | `test_playback.py` | Scaffolded with real, source-verified selectors; date-picker selector still TODO |
| Investigate | `test_investigate.py` | Scaffolded, needs test-tenant login |
| LPR | `test_lpr.py` | Scaffolded, needs a real entitled camera identified |
| PPE | `test_ppe.py` | Scaffolded, needs a real entitled camera identified |
| Camera status | `test_camera_status.py` | Scaffolded, needs test-tenant login |

## What's still needed before the first autonomous end-to-end loop

1. A dedicated staging test-tenant login (see above) — the actual
   blocker for every scaffolded test above.
2. One authenticated exploratory run (by a human, once) to confirm the
   remaining TODO selectors (date-picker, dashboard camera tiles, Live
   View video element, Investigate search box, LPR/PPE entitled camera
   ids, camera-health status indicators) against the real rendered DOM,
   replacing each `pytest.skip(...)` with a real assertion.
3. Wiring the actual autonomous loop (inspect → code → test → build →
   deploy → restart → re-run this suite → diagnose → fix → repeat) —
   this directory is the "test as a real user" piece of that loop, not
   the whole loop itself. See `docs/AUTONOMOUS_VALIDATION_PERMISSIONS.md`
   for what that loop is and isn't allowed to touch autonomously.
