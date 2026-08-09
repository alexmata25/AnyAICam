# AnyAiCam — Claude Code Project Instructions

Read @docs/CLAUDE_HANDOFF.md before making changes.

## Mission

AnyAiCam is an edge-first video-management/security platform with optional cloud services. Preserve working behavior and continue the existing codebase; do not redesign the product from scratch unless explicitly requested.

## Start every development session this way

1. Run `git status`, `git branch --show-current`, and `git log -5 --oneline`.
2. Read this file, `docs/CLAUDE_HANDOFF.md`, `DEPLOYMENT_SUMMARY.md`, `deploy/UNIFIED-PLATFORM.md`, and `appliance-agent/README.md` when relevant.
3. Inspect the current implementation before proposing changes. Do not assume historical `before-*`, `*_override.py`, phase patch, or backup files are active runtime code.
4. Establish a test baseline before editing. Record pre-existing failures separately from failures introduced by the task.
5. Make the smallest coherent change that satisfies the task.
6. Re-run relevant tests and report exact commands/results.

## Repository map

- `app/main.py` — primary FastAPI application; very large, so edit narrowly.
- `app/*.py` — cloud, customer, partner, pricing, notifications, security, storage, and related modules.
- `app/tests/` and `tests/` — automated tests.
- `appliance-agent/` — Ubuntu edge-appliance agent, systemd scripts, local discovery/configuration, queueing, and diagnostics.
- `deploy/` — environment examples and cloud/deployment documentation.
- `Dockerfile`, `Dockerfile.production`, `docker-compose.yml`, `ecs-task-definition.json` — runtime/deployment assets.
- Historical phase/build artifacts and backup files are retained for traceability; do not modify them unless the task specifically targets them.

## Architecture and product constraints

- Prefer edge processing for cameras, recording, discovery, and local operation; cloud services are optional extensions.
- Preserve support for separate edge, cloud, and combined runtime roles where the current code provides them.
- Roadmap deployment models are: DIY/local installer, managed edge appliance/mini PC, and enterprise cloud.
- The public website is primarily for acquisition/onboarding; the application/portal is for ongoing customer, subscription, device, and licensed-feature management.
- AI capabilities should remain modular/licensable rather than hard-wired into unrelated core functions.
- Preserve role isolation between public, partner, customer, and administrator experiences.
- Preserve the secure-video boundary: never expose camera usernames/passwords, RTSP URLs, or private camera IP addresses to browser/customer responses.
- Do not silently enable unfinished cloud transport, upload workers, SMS, provider contact, production provisioning, or other placeholder functionality.

## Safety and operational rules

- Never commit real passwords, API keys, tokens, private keys, camera credentials, or production connection strings. Use environment variables/placeholders and secret managers.
- Do not execute live camera/network probing, authentication, ONVIF/device contact, destructive storage operations, cloud provisioning, or production deployment unless the user explicitly authorizes that specific action and the target environment is clear.
- Development/tests should use fixtures, mocks, loopback/test networks, or passive/local evidence unless live testing is explicitly authorized.
- Never weaken authentication, authorization, customer isolation, token handling, HTTPS/cookie protections, or secret handling merely to make a test pass.
- Do not overwrite or delete working backups/history during normal feature work.

## Coding practices

- Prefer focused modules over adding more unrelated code to `app/main.py` when a clean extraction is practical and low-risk.
- Preserve public API behavior unless the task explicitly changes it.
- Add or update tests for behavior changes.
- Keep migrations/config changes backward-aware and document required environment variables.
- Avoid broad formatting/refactors mixed with functional changes.
- Explain any migration, compatibility, security, or rollback impact in the PR summary.

## Baseline commands

Use a virtual environment. Install application dependencies from `requirements.txt` and install `pytest` for development if it is not already available.

```bash
python -m pytest -q
python -m py_compile app/main.py
python -m pytest -q appliance-agent/tests
```

For deployment-related changes, also validate configuration when Docker is available:

```bash
docker compose config
```

Run narrower tests first while iterating, then the broadest practical suite before completion.

## Git workflow

- Work on a feature branch; do not push unreviewed development directly to `main`.
- Keep commits scoped and descriptive.
- Before committing, review `git diff --check` and `git diff`.
- A pull request should state: purpose, files/areas changed, test commands/results, security/deployment impact, and rollback notes.
- If the repository state or task is ambiguous, produce an assessment/plan first rather than guessing.

## Completion standard

A task is complete only when the requested behavior is implemented, relevant tests pass (or pre-existing failures are clearly identified), no secrets were introduced, safety boundaries remain intact, and the handoff/PR summary explains what changed and what should happen next.
