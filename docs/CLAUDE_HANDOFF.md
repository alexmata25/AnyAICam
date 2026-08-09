# AnyAiCam — Claude Code Handoff

Prepared for continuation of the existing AnyAiCam VMS codebase with Claude Code.

## Source of truth

Repository: `alexmata25/AnyAICam`

Default branch: `main`

Handoff baseline on 2026-08-08: `1eee3769454df71bdb71cd9cc91d1a525cae2e28`

Before doing any work, fetch/pull and confirm whether `main` has advanced beyond this commit. Treat the newest reviewed `main` as source of truth unless the user identifies another branch or artifact.

## What this repository already contains

The repository is an existing Python/FastAPI VMS/security platform, not a greenfield project. It includes:

- a large primary FastAPI application in `app/main.py`;
- customer, partner, pricing, notification, security, database, storage, cloud, and appliance modules under `app/`;
- camera/VMS-related dependencies including OpenCV, Ultralytics, and FFmpeg integration;
- Docker and production deployment assets;
- AWS/cloud preparation and separate edge/cloud/combined runtime concepts;
- a unified public/partner/customer platform and API foundation;
- an Ubuntu appliance agent under `appliance-agent/` with service scripts, discovery, setup wizard, queueing, metrics, and diagnostics;
- test suites under both `tests/` and `app/tests/` plus appliance-agent tests;
- historical phase, override, backup, manifest, checksum, and verification files retained for traceability.

Do not start over. Inspect and continue the active implementation.

## Current architectural direction

The intended product direction is edge-first with optional cloud services.

Target deployment models:

1. DIY/local installation on a customer Windows/Linux machine where appropriate.
2. Managed AnyAiCam edge appliance/mini PC.
3. Enterprise/cloud deployment for customers that require it.

The website is primarily for acquisition/onboarding. The application/portal handles ongoing users, sites, cameras, subscriptions, licensed features, alerts, recordings, and administration.

AI features should be modular/licensable capabilities rather than tightly coupling every workflow to one AI provider.

The Ryzen 5 mini PC is the primary near-term Ubuntu development/troubleshooting appliance. Raspberry Pi 5 is a later optimization/validation target once the software is stable, especially for lower-cost 8–16-camera cloud-first deployments.

## Existing security boundaries that must be preserved

- Customer/browser responses must not expose camera usernames, camera passwords, RTSP URLs, or private camera IP addresses.
- Role isolation between public, partner, customer, and administrator experiences must remain enforced.
- Do not weaken authentication, authorization, CSRF, token/session, cookie, HTTPS, or customer/site/camera permission controls to make development easier.
- Do not commit secrets. Use environment variables, example files, placeholders, and secret managers.
- Do not silently enable cloud video transport, uploads, SMS, provider contact, production provisioning, or other features currently represented as disabled/preparation-only.
- Do not add a remote shell/backdoor to the appliance agent.

## Live-device/network safety

Default development is non-invasive.

Unless the user explicitly authorizes a specific live test and target environment, do not:

- scan arbitrary networks;
- authenticate to real cameras/NVRs;
- perform ONVIF or vendor-device queries against real equipment;
- change camera/NVR settings;
- persist camera credentials;
- activate cloud services/providers;
- provision or modify production AWS resources;
- delete recordings/customer data or perform destructive migrations.

Use mocks, fixtures, test networks, loopback, sample data, or passive/local evidence for normal development.

## Important repository reality

`app/main.py` is very large. Avoid casual whole-file rewrites. Make narrow changes and prefer extracting clearly bounded new functionality into modules when doing so is safe and does not alter public behavior.

Numerous files named `before-*`, `*_override.py`, phase extensions, and historical backups exist. They are context/history, not automatically active runtime code. Confirm imports/entrypoints before editing them.

The root `Dockerfile` currently runs Python 3.12, installs FFmpeg, installs `requirements.txt`, copies `app/`, and starts `uvicorn main:app` on port 8000.

The root requirements currently include FastAPI, Uvicorn, psycopg, boto3, passlib/bcrypt, multipart, itsdangerous, Ultralytics, OpenCV, and pywebpush.

The Ubuntu appliance agent has its own `pyproject.toml`, command-line entry points, systemd unit/scripts, and tests.

## Recorded project state

The repository contains a `V1_1_2_PREFLIGHT_MANIFEST.json` describing a `1.1.2-preflight` repair artifact for cloud-settings imports, CSRF fetch-wrapper ordering, and lower-latency HLS settings. Treat it as historical/release metadata rather than proof of the current runtime version.

`DEPLOYMENT_SUMMARY.md` documents a later AWS deployment-foundation stage with local/staging/production identity and edge/cloud/combined runtime roles while keeping cloud video/upload behavior disabled.

`deploy/UNIFIED-PLATFORM.md` documents the shared public, partner, customer and mobile/API foundation and the secure-video boundary.

`VALIDATION.txt` records PASS results for customer portal, per-camera settings, entitlement enforcement, admin entitlement page, PWA/mobile artifacts, push enrollment, and selected compilation checks. Re-run tests yourself; do not rely on the recorded file as a fresh CI result.

## First-session procedure for Claude Code

Perform these steps before coding:

```bash
git status
git branch --show-current
git log -5 --oneline
python --version
ffmpeg -version || true
```

Create/use a virtual environment and install development dependencies without writing secrets into the repository.

Then run a baseline. At minimum:

```bash
python -m pytest -q
python -m py_compile app/main.py
python -m pytest -q appliance-agent/tests
```

If the full suite is too slow or environment-dependent, run the relevant suites separately and report exactly which tests were run, skipped, or blocked.

For Docker/deployment work when Docker is installed:

```bash
docker compose config
```

Do not fix unrelated pre-existing failures without first identifying them as pre-existing.

## Development workflow

For every requested feature/fix:

1. Restate the exact task and affected subsystem.
2. Inspect the current implementation and relevant tests.
3. Identify security, compatibility, migration, hardware/network, and deployment implications.
4. Make the smallest coherent implementation.
5. Add/update tests.
6. Run focused tests, then the broadest practical regression suite.
7. Review `git diff --check` and `git diff`.
8. Commit on a feature branch with a descriptive message.
9. Prepare a PR summary containing purpose, implementation, tests/results, risks, deployment impact, and rollback notes.

Do not push experimental work directly to `main`.

## Near-term development priorities

When the user asks to continue the VMS generally rather than requesting a specific feature, do not invent a new phase. First assess the current repository and propose the next bounded milestone.

Likely high-value areas to evaluate include:

- stabilizing the Ubuntu/Ryzen edge-appliance development environment;
- establishing a clean reproducible local run/test path;
- reducing risk created by the monolithic `app/main.py` without broad rewrites;
- validating local camera/discovery/recording behavior safely on explicitly authorized test equipment;
- clarifying appliance-to-cloud transport boundaries before enabling remote video features;
- strengthening CI, release manifests, migrations, backup/rollback, and observability;
- keeping AI analytics modular and separately licensed/configured.

Any real camera testing, network discovery, cloud activation, or production deployment requires explicit user authorization for that action.

## Handoff rule for future AI agents

Keep `CLAUDE.md` short and operational. Put durable architecture/current-state detail in this file. After a significant milestone, update this handoff with:

- current reviewed branch/commit;
- what was completed;
- test baseline/results;
- new environment requirements;
- known issues/technical debt;
- next recommended bounded task;
- safety or rollout constraints.

This makes the project portable between Claude Code, Codex, and other coding agents without relying on conversation memory.
