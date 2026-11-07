# Windows VMS 1.3.4.1: session-file rollback trust boundary (owner decision 2026-11-06, Option A)

**Status: OPEN security item.** It must be resolved before customer deployment. This page documents the boundary for
the current test candidate only; it is not a fix.

## What the session files are
- `sessions.json` (in the VMS data folder, beside `users.json`) holds every VMS sign-in session: the VMS sign-in
  page and the administrator sign-in of a local account. Partner Portal sessions are stored separately, in the
  portal database; nothing on this page applies to them.
- `sessions_epoch.json` holds the current session epoch. A new epoch is written whenever a damaged `sessions.json` is
  quarantined, and only sessions from the current epoch are accepted.

## What is still possible (Codex review of a0a74ea, Part B items 1 and 2, Medium)
1. Putting back an older, undamaged `sessions.json` revives sessions that were signed out normally since that copy.
2. Deleting `sessions_epoch.json`, together with an old session record put back into `sessions.json`, revives sessions
   from before the first quarantine.

Both need write access to the data folder.

## What is already refused
- A disabled account is refused, whatever `sessions.json` says.

## Not yet verified: sessions after a password reset
- Candidate r5 records an administrator's password reset as a per-account session counter in `users.json`, and a
  VMS session is accepted only while it carries the account's current counter. It is meant to refuse every earlier
  session, even if an older `sessions.json` is put back. It replaces the r4 time-based cutoff, which the Codex
  review of 20b3a35 found could fail if the clock stepped backward.
- This protection is **not** established until its focused tests and an independent review pass. Even then it does
  **not** close items 1 and 2: a normal sign-out is still undone by an older `sessions.json`.

## Trust boundary
- **Anyone who can write the VMS data folder is trusted with sign-in state.** Such a writer can already edit
  `users.json` (add an administrator, replace a password hash), so items 1 and 2 give them nothing more.
- **The realistic risk is an operator restoring the data folder from an older file-level backup.** Sessions signed out
  since the backup could sign in again until they expire (at most 30 days for "remember me").
- The VMS's own configuration restore (Operations, Restore) never restores `sessions.json` or `sessions_epoch.json`.

## Required after restoring the data folder from an older backup
After any file-level restore of the VMS data folder (copying an older `sessions.json`, or the whole folder, back),
**invalidate every session before users return**:
1. Stop the AnyAiCam VMS service.
2. Delete `sessions.json` from the data folder. Leave `sessions_epoch.json` in place.
3. Start the service. Every user signs in to the VMS again; no earlier VMS session is accepted.

This step does not sign anyone out of the Partner Portal, whose sessions are stored separately. No procedure for
them is part of Option A.

Never put back `sessions.json` or `sessions_epoch.json` on their own.

## Resolution before customer deployment
Options B (a new epoch on every service start, the epoch always required) and C (a trust anchor outside the data
folder) are described in the owner proposal of 2026-11-07. Neither is implemented. The item stays open until one is
approved, implemented and reviewed, or the owner accepts the boundary for customer deployment.
