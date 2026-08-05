# AnyAiCam Phase 6F

## Exact phase

**Phase 6F — Controlled Production Validation and Customer Pilot**

Phase 6F adds a tightly bounded pilot-management layer after Phase 6E operational assurance.

### Features

- Pilot cohorts limited by `PHASE6F_MAX_PILOT_CUSTOMERS` (default 10)
- Draft, ready, active, paused, completed, rolled-back, and cancelled states
- Mandatory validation checklist
- Two-person approval gate
- Phase 6E health dependency
- Explicit rollback triggers
- Pilot event timeline
- Approval and validation evidence
- Administrator-only dashboard and APIs
- No provider contact or deployment action

## Apply

First apply Phase 6E. Then place these Phase 6F files beside the resulting `main.py` and run:

```bash
python apply_phase6f.py main.py
```

The patcher refuses to run unless it finds the Phase 6E marker.

## Test

```bash
pytest -q test_phase6f_static.py
python -m py_compile main.py
```

## Routes

- `GET /pilots`
- `GET /api/pilots`
- `POST /api/pilots`
- `GET /api/pilots/{pilot_id}`
- `POST /api/pilots/{pilot_id}/approvals`
- `POST /api/pilots/{pilot_id}/validations`
- `PUT /api/pilots/{pilot_id}/status`

## Readiness requirements

A pilot cannot enter `ready` or `active` unless:

1. Cohort size is within the configured limit.
2. Every required validation passes.
3. Two distinct approvals are active.
4. The Phase 6E operational snapshot is healthy.

Changing a pilot to `active` only updates local pilot state. It does not deploy software, provision cloud resources, activate cameras, or contact a provider.
