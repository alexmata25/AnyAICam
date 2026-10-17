# AnyAiCam website: 1.2.7 release builders (version-controlled copy)

These are the scripts that turn the live-site snapshot into the prepared 1.2.7 pages. They run from the working
folder `anyaicam-green/website-1.2.7-prep/` (the base pages and the live snapshot live there, not in this repo);
this folder is the reviewed, backed-up copy and must stay byte-identical to it (see `SHA256SUMS`).

| Script | What it does |
|---|---|
| `build_127_pages.py`, `build_127_home.py`, `build_127_signup_links.py`, `build_127_header.py` | 1.2.7 download pages, home page, sign-up/plan links, mobile header |
| `build_127_pricing.py` | 2026-10-17: Hybrid $19.99; licenses $24.99/$37.49/$62.49/$99.99; free phone support on all three plans; plan comparison in everyday language (remote viewing on all plans, Cloud Video Backup on Hybrid only, how long recordings are kept). Exact-match edits only; `--dry-run` writes nothing. |
| `verify_publishable.py` | Refuses publication while anything is pending, an old price or old wording remains, or a comparison mark is wrong |
| `refresh_for_candidate.sh` | Rebuilds the pending export for a verified candidate installer |
| `finalize_download_host.sh` | Switches the pages to published (only after the owner approves) |

Nothing here publishes anything. Publication needs the owner's explicit approval.
