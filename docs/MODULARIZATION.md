# AnyAICam v1.2 Modularization

## Goal

Reduce risk in the production application by extracting one tested responsibility at a time from `app/main.py`. The production entry point remains `app.main:app` until a later, separately verified cutover.

## Module ownership

- `app.api`: FastAPI routers, schemas, authorization dependencies, and HTTP helpers.
- `app.services`: application workflows and orchestration.
- `app.edge`: appliance communication, discovery, verification, and stream coordination.
- `app.drivers`: vendor, FFmpeg, database, and object-storage adapters.
- `app.platform_core`: dependency-light architecture metadata and shared foundations.

## Migration rules

1. Do not append new unrelated features to `app/main.py`.
2. Extract only one responsibility per change.
3. Preserve route paths, response formats, authorization checks, and environment-variable names.
4. Add characterization tests before replacing existing behavior.
5. Keep customer, administrator, and partner portals operational during every extraction.
6. Do not contact real cameras, run network discovery, activate cloud services, or persist credentials in tests.
7. Keep rollback possible by limiting each extraction to a small commit or pull request.

## First functional slice

`app.api.http_range` provides strict single-byte-range parsing for browser media playback. It is intentionally not wired into production yet. The safe integration sequence is:

1. Identify the existing recording-download endpoint in `app/main.py`.
2. Add characterization tests for its current authorization and not-found behavior.
3. Replace only its range parsing with `parse_single_range`.
4. Return `206 Partial Content`, `Accept-Ranges: bytes`, a correct `Content-Range`, and the existing media MIME type.
5. Verify seeking and playback in Chrome, Safari, Firefox, and a mobile browser.

## Verification command

From the `app` directory:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

The initial foundation adds tests for the module registry and HTTP byte-range parser. No production route has been changed in this phase.
