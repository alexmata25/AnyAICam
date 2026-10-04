"""Bluehost upload package for the public website (website/). Never uploads.

    python tools/website_publish_package.py build --out website-publish-3
    python tools/website_publish_package.py build --out website-publish-3 --allow-pending   # preparation only

`build` reads the live anyaicam.com files over HTTPS (GET only; PHP endpoints and
.htaccess are never requested, because requesting a PHP file runs it), compares each
with website/, and writes:

    <out>/upload/              the files that change on the host, in public_html layout
    <out>/rollback-original/   the host source of each replaced file (from git history),
                               or the live bytes when no repository version matches
    <out>/SHA256SUMS.txt       checksums of upload/
    <out>/manifest.json        every runtime file with its status and checksum
    <out>/PUBLISH.md           the owner's upload checklist

Live HTML passes through Cloudflare, which injects an analytics beacon and rewrites
e-mail addresses. Those differences are ignored, and only the repository source is
ever uploaded or kept for rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import website_launch_check  # noqa: E402
import website_release  # noqa: E402

WEBSITE = website_release.WEBSITE
ROOT = WEBSITE.parent
LIVE = "https://anyaicam.com/"
# Repository files that are not part of the site (website/README.md explains each).
REPO_ONLY = {"README.md", "SOURCE_MANIFEST.json", "SERVER_CLEANUP.json", ".gitignore", ".gitattributes",
             "stripe-config.example.php"}
# Server-side files: never requested over HTTP. Compared with the host export instead.
HOST_EXPORT = "040664b"  # website: replace the HTTP mirror with the real Bluehost source
TEXT = (".html", ".css", ".js", ".xml", ".txt", ".webmanifest", ".json")


def runtime_files(website: Path = WEBSITE) -> list[str]:
    """Every file that belongs in public_html, as a sorted list of relative paths."""
    files = []
    for path in website.rglob("*"):
        rel = path.relative_to(website).as_posix()
        if path.is_file() and path.name not in REPO_ONLY and path.suffix.lower() != ".md":
            files.append(rel)
    return sorted(files)


def server_side(rel: str) -> bool:
    return rel.endswith(".php") or rel.rsplit("/", 1)[-1].startswith(".ht")


def normalize(data: bytes, rel: str) -> bytes:
    """Content with Cloudflare's injected beacon and e-mail obfuscation removed."""
    if not rel.endswith(TEXT):
        return data
    text = data.decode("utf-8", "replace")
    text = re.sub(r"<script\b[^>]*(?:cloudflareinsights|email-decode|data-cf-beacon)[^>]*>\s*</script>", " ", text)
    text = re.sub(r"<a\b[^>]*(?:/cdn-cgi/l/email-protection|mailto:)[^>]*>.*?</a>", "EMAIL", text, flags=re.S)
    text = re.sub(r'<span class="__cf_email__".*?</span>', "EMAIL", text, flags=re.S)
    text = re.sub(r"/cdn-cgi/l/email-protection#[0-9a-f]*", "EMAIL", text)
    text = re.sub(r"\[email(?:&#160;|\xa0| )protected\]", "EMAIL", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "EMAIL", text)
    return re.sub(r"\s*([<>])\s*", r"\1", re.sub(r"\s+", " ", text)).strip().encode()


def fetch(rel: str) -> tuple[int, bytes]:
    request = urllib.request.Request(LIVE + quote(rel), headers={"User-Agent": "AnyAiCam-website-package/1.0 (read-only)"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def git_versions(rel: str) -> list[bytes]:
    commits = subprocess.run(["git", "log", "--format=%H", "--", f"website/{rel}"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    out = []
    for commit in commits:
        shown = subprocess.run(["git", "show", f"{commit}:website/{rel}"], cwd=ROOT, capture_output=True)
        if shown.returncode == 0:
            out.append(shown.stdout)
    return out


def classify(rel: str, current: bytes, fetcher=fetch, history=git_versions) -> dict:
    """Status of one runtime file relative to the host."""
    exported = subprocess.run(["git", "show", f"{HOST_EXPORT}:website/{rel}"], cwd=ROOT, capture_output=True)
    unchanged_since_export = exported.returncode == 0 and exported.stdout == current
    if server_side(rel):
        if unchanged_since_export:
            return {"status": "on host, unchanged since the host export"}
        return {"status": "server-side change", "upload": True, "special": "server-side file; not readable over HTTP"}
    code, live = fetcher(rel)
    if code == 404:
        return {"status": "new", "upload": True}
    if code != 200:
        return {"status": f"live returned {code}", "special": "check by hand"}
    if live == current or normalize(live, rel) == normalize(current, rel):
        return {"status": "same as live"}
    if unchanged_since_export:  # the host already has this exact file; the CDN serves something else
        return {"status": "on host, unchanged since the host export",
                "special": "the live URL serves different bytes from the host file (CDN); not uploaded"}
    entry = {"status": "changed", "upload": True, "live": live}
    original = next((old for old in history(rel) if normalize(old, rel) == normalize(live, rel)), None)
    if original is None:
        entry["special"] = "live matches no repository version; rollback keeps the live bytes"
    entry["rollback"] = original if original is not None else live
    return entry


def build(out: Path, allow_pending: bool = False, website: Path = WEBSITE, fetcher=fetch, history=git_versions) -> dict:
    failures, summary = website_launch_check.run(website)
    if failures:
        raise SystemExit("launch check failed; fix these first:\n" + "\n".join(failures))
    state = website_release.check(website)[0]
    if state != "published" and not allow_pending:
        raise SystemExit("the release blocks are still pending; run website_release.py publish first "
                         "(or pass --allow-pending for a preparation build)")
    if out.exists():
        shutil.rmtree(out)
    manifest = {"release": summary, "files": {}}
    sums = []
    for rel in runtime_files(website):
        current = (website / rel).read_bytes()
        entry = classify(rel, current, fetcher, history)
        sha = hashlib.sha256(current).hexdigest()
        record = {"status": entry["status"], "sha256": sha}
        if entry.get("special"):
            record["special"] = entry["special"]
        if entry.get("upload"):
            target = out / "upload" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current)
            sums.append(f"{sha}  {rel}")
            record["upload"] = True
        if "rollback" in entry:
            target = out / "rollback-original" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry["rollback"])
        manifest["files"][rel] = record
    cleanup = json.loads((website / "SERVER_CLEANUP.json").read_text(encoding="utf-8"))["remove_from_public_html"]
    manifest["delete_from_host"] = [rel for rel in cleanup if server_side(rel) or fetcher(rel)[0] == 200]
    out.mkdir(parents=True, exist_ok=True)
    (out / ".gitattributes").write_text("* -text\n", encoding="utf-8")  # keep every byte as uploaded
    (out / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "PUBLISH.md").write_text(checklist(manifest, state), encoding="utf-8")
    return manifest


def checklist(manifest: dict, state: str) -> str:
    files = manifest["files"]
    upload = [rel for rel, r in files.items() if r.get("upload")]
    new = [rel for rel in upload if files[rel]["status"] == "new"]
    special = [f"- `{rel}`: {r['special']}" for rel, r in files.items() if r.get("special")]
    lines = [
        "# anyaicam.com upload package",
        "",
        f"Release blocks: **{manifest['release']}**."
        + (" This is a preparation build: do not upload it. Run `website_release.py publish` with the final"
           " installer, then rebuild this package." if state != "published" else ""),
        "",
        f"## Upload ({len(upload)} files)",
        "Copy `upload/` into `public_html`, keeping the folder layout. Nothing is deleted from the host.",
        "",
        *[f"- `{rel}`" + (" (new)" if rel in new else "") for rel in upload],
        "",
        "## Not uploaded",
        f"- {sum(1 for r in files.values() if r['status'] == 'same as live')} files already match the live site.",
        f"- {sum(1 for r in files.values() if r['status'].startswith('on host'))} files (PHP endpoints, .htaccess and"
        " unchanged assets) are already on the host exactly as exported.",
        "- Never upload repository notes (README.md, SOURCE_MANIFEST.json, SERVER_CLEANUP.json), example configs,"
        " tests, tools or docs. Secrets (config.php, mail_config.php, stripe-config.php, api.php) stay on the host.",
        "",
        "## Special handling",
        *(special or ["- None."]),
        "- If `ARCHIVED-0.1.3-DOWNLOADS.md` exists in `public_html`, delete it (internal note with 0.1.3 links).",
        "",
        "## Delete from public_html (internal test pages and snippets, see website/SERVER_CLEANUP.json)",
        "Move them outside `public_html` if you want to keep them. PHP files cannot be checked over HTTP;"
        " delete them if present.",
        "",
        *([f"- `{rel}`" + (" (PHP: check in File Manager)" if server_side(rel) else " (still served)")
           for rel in manifest.get("delete_from_host", [])] or ["- None still served."]),
        "",
        "## After uploading",
        "1. Purge the Cloudflare cache.",
        "2. Run `python tools/website_publish_package.py build --out <new folder>`: every file should report"
        " `same as live`.",
        "",
        "## Rollback",
        "Upload `rollback-original/` (the host source of every replaced file) and delete the files marked (new).",
        "Checksums of `upload/`: `SHA256SUMS.txt`.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--allow-pending", action="store_true")
    sub.add_parser("files", help="list the runtime files that belong in public_html")
    args = parser.parse_args(argv)
    if args.command == "files":
        print("\n".join(runtime_files()))
        return 0
    manifest = build(args.out, args.allow_pending)
    upload = sum(1 for r in manifest["files"].values() if r.get("upload"))
    print(f"{len(manifest['files'])} runtime files; {upload} to upload; package in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
