"""Release-specific copy on the public website (website/), in one place.

Every line of the website that names an AnyAiCam installer release sits
between markers:

    <!--release:ID state=pending-->...<!--/release:ID-->
    <!--release:ID state=published version=X filename=Y sha256=Z url=U [size=BYTES]-->...<!--/release:ID-->

and is rendered only from the templates below, so the final release is a
single command once the installer exists:

    python tools/website_release.py publish --version 1.2.3 \
        --filename <installer .tar.gz> --sha256 <64 hex> [--installer <path>] [--url <download page>]
    python tools/website_release.py pending     # back to "release finalizing"
    python tools/website_release.py check       # used by tools/website_launch_check.py

--installer recomputes the SHA-256, checks the file name against the real
file and records its size. --url is where customers download it: since
2026-10-05 the installer file itself, served from the website (activating
AnyAiCam still needs an account with a VMS licence). Superseded installers
are refused. Nothing is uploaded or published:
this only edits website/ in the working tree.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
DEFAULT_URL = "https://app.anyaicam.com/customer-login.html"
REGISTER = "https://app.anyaicam.com/customer-register"
FIELDS = ("version", "filename", "sha256", "url", "size")  # size (bytes) is optional

# Installers that must never be named on the website again.
SUPERSEDED_SHA256 = {
    "5729f5ded4331b01e7cbe3b4f86afc55051fa68b04e02e6c04d8d4c6b57ce29e",  # 1.2.3 @ 9f59f11 (historical candidate)
}
# 1.2.3 @ 9f59f11; 1.2.2 @ 7ed690c (never publish); 1.2.3 @ f1862e1 (wrong source line)
SUPERSEDED_SHA256_PREFIXES = ("5729f5de", "667ac892", "0486feb7")
SUPERSEDED_FILENAME_PARTS = ("9f59f11561d9", "f1862e1", "7ed690c84e73", "ecf229892f2b", "-1.2.2-", "-0.1.3")
SUPERSEDED_VERSIONS = {"1.2.2", "0.1.3"}

BLOCKS = {
    "vms.html": {
        "vms-card-status": (
            '<span class="pill future">Release finalizing</span>\n'
            '        <h3>AnyAiCam for Linux</h3>',
            '<span class="pill">Available now</span>\n'
            '        <h3>AnyAiCam {version} for Linux</h3>',
        ),
        "vms-card-download": (
            '<div class="button-row" style="margin-top:.6rem">\n'
            f'          <a class="button primary" href="{REGISTER}">Create an account</a>\n'
            f'          <a class="button light" href="{DEFAULT_URL}">Sign in</a>\n'
            '        </div>\n'
            '        <p style="font-size:.9rem;color:#64748b;margin-top:.7rem">The Linux installer is being finalized. '
            'When it is released, you download it from <strong>My subscription</strong>, and its version and SHA-256 '
            'are listed here. <a href="vms-linux.html" style="color:#16778b;text-decoration:underline;font-weight:700">Installation guide</a></p>',
            '<div class="button-row" style="margin-top:.6rem">\n'
            '          <a class="button primary" href="{url}" download>Download for Linux</a>\n'
            f'          <a class="button light" href="{REGISTER}">Create an account</a>\n'
            '        </div>\n'
            '        <p style="font-size:.9rem;color:#64748b;margin-top:.7rem">Activating AnyAiCam needs an AnyAiCam account with a VMS licence. '
            '<a href="vms-linux.html" style="color:#16778b;text-decoration:underline;font-weight:700">Installation guide</a></p>\n'
            '        <p style="font-size:.82rem;color:#64748b;margin-top:.4rem;overflow-wrap:anywhere">Version {version} · Ubuntu 24.04 · x86_64{size_text} · SHA-256 <code>{sha256}</code></p>',
        ),
    },
    "vms-linux.html": {
        "linux-hero-actions": (
            f'<a class="button primary" href="{REGISTER}">Create an account</a>\n'
            f'        <a class="button light" href="{DEFAULT_URL}">Sign in</a>',
            '<a class="button primary" href="{url}" download>Download for Linux</a>\n'
            f'        <a class="button light" href="{REGISTER}">Create an account</a>',
        ),
        "linux-download-step": (
            'Sign in to the AnyAiCam portal, open <strong>My subscription</strong> and select <strong>Download installer</strong>.',
            'Download the installer, <a href="{url}" download><code style="overflow-wrap:anywhere">{filename}</code></a>{size_paren}. '
            'Activating AnyAiCam needs an AnyAiCam account with a VMS licence.',
        ),
        "linux-hero-release": (
            '<h2>Release status</h2>\n'
            '      <p><strong>AnyAiCam for Linux</strong><br>Ubuntu 24.04 · x86_64</p>\n'
            '      <p style="font-size:.85rem">Release finalizing. The version and SHA-256 are listed here when the installer is released.</p>',
            '<h2>This release</h2>\n'
            '      <p><strong>AnyAiCam {version}</strong><br>Ubuntu 24.04 · x86_64{size_text}</p>\n'
            '      <p style="overflow-wrap:anywhere;font-size:.85rem">SHA-256<br><code>{sha256}</code></p>',
        ),
        "linux-install-status": (
            '<div class="notice" style="margin-bottom:1rem;max-width:62rem"><strong>Release finalizing:</strong> '
            'the Linux installer is not available to download yet. These steps apply once it appears in '
            '<strong>My subscription</strong>.</div>',
            '',
        ),
        "linux-checksum-step": (
            'Check the download is complete. In a terminal, in the folder you saved it to, run the first command below. '
            'The result must match the SHA-256 shown for the release.',
            'Check the download is complete. In a terminal, in the folder you saved it to, run the first command below. '
            'The result must be <code style="overflow-wrap:anywhere">{sha256}</code>.',
        ),
        "linux-commands": (
            'sha256sum anyaicam-appliance-installer-*.tar.gz\n'
            'mkdir -p ~/anyaicam-installer\n'
            'tar -xzf anyaicam-appliance-installer-*.tar.gz -C ~/anyaicam-installer',
            'sha256sum {filename}\n'
            'mkdir -p ~/anyaicam-installer\n'
            'tar -xzf {filename} -C ~/anyaicam-installer',
        ),
    },
    "index.html": {
        "home-linux-note": (
            'AnyAiCam appliances from $949.99. AnyAiCam for your own Ubuntu 24.04 PC: release finalizing.',
            'AnyAiCam appliances from $949.99, or run AnyAiCam on your own Ubuntu 24.04 PC.',
        ),
    },
}

MARKER = re.compile(r"<!--release:([a-z0-9-]+)((?: [a-z0-9]+=[^\s>]+)*)-->(.*?)<!--/release:\1-->", re.S)
ATTR = re.compile(r" ([a-z0-9]+)=([^\s>]+)")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FILENAME = re.compile(r"^anyaicam-[A-Za-z0-9._-]+\.tar\.gz$")


class ReleaseError(ValueError):
    pass


def validate_values(values: dict) -> dict:
    version, filename, sha, url = values["version"], values["filename"], values["sha256"].lower(), values["url"]
    if not VERSION.match(version):
        raise ReleaseError(f"version must look like 1.2.3, got {version!r}")
    if version in SUPERSEDED_VERSIONS:
        raise ReleaseError(f"version {version} is superseded and must never be published")
    if not FILENAME.match(filename) or "--" in filename:
        raise ReleaseError(f"installer file name must be anyaicam-*.tar.gz, got {filename!r}")
    if f"-{version}-" not in filename:
        raise ReleaseError(f"installer file name {filename!r} does not carry version {version}")
    if any(part in filename for part in SUPERSEDED_FILENAME_PARTS):
        raise ReleaseError(f"installer file name {filename!r} is a superseded build")
    if not SHA256.match(sha):
        raise ReleaseError("SHA-256 must be 64 hex characters")
    if sha in SUPERSEDED_SHA256 or sha.startswith(SUPERSEDED_SHA256_PREFIXES):
        raise ReleaseError("SHA-256 belongs to a superseded installer")
    if not re.match(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/%-]*)?$", url) or "--" in url:
        raise ReleaseError(f"download URL must be a plain https URL, got {url!r}")
    if url.endswith(".tar.gz") and not url.endswith("/" + filename):
        raise ReleaseError(f"download URL {url!r} is a different file than {filename!r}")
    checked = {"version": version, "filename": filename, "sha256": sha, "url": url}
    size = str(values.get("size") or "")
    if size:
        if not size.isdigit() or int(size) <= 0:
            raise ReleaseError(f"size must be a positive number of bytes, got {size!r}")
        checked["size"] = size
    return checked


def _megabytes(size: str) -> str:
    return f"{int(size) / 1_000_000:.1f} MB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render(block_id: str, templates: tuple, values: dict | None) -> str:
    if values is None:
        return f"<!--release:{block_id} state=pending-->{templates[0]}<!--/release:{block_id}-->"
    attrs = " ".join(f"{key}={values[key]}" for key in FIELDS if key in values)
    size = values.get("size")
    fields = dict(values, size_text=f" · {_megabytes(size)}" if size else "", size_paren=f" ({_megabytes(size)})" if size else "")
    return f"<!--release:{block_id} state=published {attrs}-->{templates[1].format(**fields)}<!--/release:{block_id}-->"


def _read(path: Path) -> tuple[str, str]:
    raw = io.open(path, encoding="utf-8", newline="").read()
    newline = "\r\n" if raw.count("\r\n") > raw.count("\n") / 2 else "\n"
    return newline, raw.replace("\r\n", "\n")


def apply(values: dict | None, website: Path = WEBSITE) -> list[str]:
    changed = []
    for name, blocks in BLOCKS.items():
        newline, text = _read(website / name)
        found = {match.group(1) for match in MARKER.finditer(text)}
        missing = set(blocks) - found
        if missing:
            raise ReleaseError(f"{name}: release markers missing: {sorted(missing)}")
        updated = MARKER.sub(lambda m: render(m.group(1), blocks[m.group(1)], values) if m.group(1) in blocks else m.group(0), text)
        if updated != text:
            io.open(website / name, "w", encoding="utf-8", newline="").write(updated.replace("\n", newline))
            changed.append(name)
    return changed


def check(website: Path = WEBSITE) -> tuple[str, dict | None, list[str]]:
    """Every block present, rendered from its template, and all in one state."""
    problems, states = [], set()
    for name, blocks in BLOCKS.items():
        _, text = _read(website / name)
        seen = []
        for match in MARKER.finditer(text):
            block_id, attrs = match.group(1), dict(ATTR.findall(match.group(2)))
            seen.append(block_id)
            if block_id not in blocks:
                problems.append(f"{name}: unknown release block {block_id}")
                continue
            state = attrs.pop("state", None)
            values = None
            if state == "published":
                try:
                    values = validate_values({key: attrs.get(key, "") for key in FIELDS})
                except ReleaseError as exc:
                    problems.append(f"{name}:{block_id}: {exc}")
                    continue
                states.add(tuple(sorted(values.items())))
            elif state == "pending":
                states.add("pending")
            else:
                problems.append(f"{name}:{block_id}: unknown state {state!r}")
                continue
            if match.group(0) != render(block_id, blocks[block_id], values):
                problems.append(f"{name}:{block_id}: edited by hand; re-render with tools/website_release.py")
        for block_id in sorted(set(blocks) - set(seen)):
            problems.append(f"{name}: release block {block_id} missing")
        for block_id in sorted({b for b in seen if seen.count(b) > 1}):
            problems.append(f"{name}: release block {block_id} appears more than once")
    if len(states) > 1:
        problems.append("release blocks disagree (mixed pending/published or different release values)")
    if len(states) == 1 and states != {"pending"}:
        return "published", dict(next(iter(states))), problems
    return "pending", None, problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("pending", help="show 'release finalizing' everywhere")
    sub.add_parser("check", help="verify the release blocks")
    pub = sub.add_parser("publish", help="name the final installer everywhere")
    pub.add_argument("--version", required=True)
    pub.add_argument("--filename", required=True)
    pub.add_argument("--sha256", required=True)
    pub.add_argument("--url", default=DEFAULT_URL, help=f"where customers download it (default {DEFAULT_URL})")
    pub.add_argument("--installer", type=Path, help="the real installer: its name and SHA-256 must match")
    pub.add_argument("--website", type=Path, default=WEBSITE, help=argparse.SUPPRESS)
    for name in ("pending", "check"):
        sub.choices[name].add_argument("--website", type=Path, default=WEBSITE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.mode == "check":
            state, values, problems = check(args.website)
            for problem in problems:
                print("FAIL", problem)
            print(f"release blocks: {state}" + (f" {values['version']} {values['filename']} {values['sha256']}" if values else ""))
            return 1 if problems else 0
        values = None
        if args.mode == "publish":
            values = validate_values({"version": args.version, "filename": args.filename, "sha256": args.sha256, "url": args.url})
            if args.installer:
                if args.installer.name != values["filename"]:
                    raise ReleaseError(f"--filename {values['filename']} != installer {args.installer.name}")
                actual = sha256_of(args.installer)
                if actual != values["sha256"]:
                    raise ReleaseError(f"--sha256 does not match the installer (actual {actual})")
                values["size"] = str(args.installer.stat().st_size)
        changed = apply(values, args.website)
        print(("published " + values["version"]) if values else "pending", "->", ", ".join(changed) or "no change")
        state, _, problems = check(args.website)
        for problem in problems:
            print("FAIL", problem)
        return 1 if problems else 0
    except ReleaseError as exc:
        print("REFUSED:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
