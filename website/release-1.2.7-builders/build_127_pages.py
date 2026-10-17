"""Prepare anyaicam.com's VMS download pages for Linux 1.2.7 + the Windows 1.3.0 preview (Green, 2026-10-16).
DRAFT: nothing is uploaded. Base = the SERVER originals of vms.html / vms-linux.html (cPanel, 2026-10-15),
which the live pages still match on 2026-10-16 apart from Cloudflare's email rewriting; re-check against the
server before publishing.

Every change is an exact, single-occurrence replacement; anything unexpected aborts. Values not yet known
(installer SHA-256, size, final URL) are PENDING tokens, and verify_publishable.py refuses pages containing any.

    py -3.11 build_127_pages.py [--sha256 HEX --size BYTES] [--url-base https://anyaicam.com/downloads/linux/1.2.7]
"""
import argparse
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).parent
BASES = HERE / "base"
BASE_SHA = {"vms.html": "f42d94ac56b4", "vms-linux.html": "21d7fdcd5f94"}  # sha256 prefixes of the server originals

VERSION = "1.2.7"
FILENAME = "anyaicam-appliance-installer-1.2.7-vms-1e75f42be8bd.tar.gz"  # Red, 2026-10-16 (commit 1e75f42be8bd)
OLD = {"version": "1.2.4", "filename": "anyaicam-appliance-installer-1.2.4-vms-3b2317e8e532.tar.gz",
       "sha256": "5eb86422fdca69412249533716e2251243a055225746c020c57b6406f3bf3077", "size": 35341374,
       "url": "https://anyaicam.com/downloads/linux/1.2.4/anyaicam-appliance-installer-1.2.4-vms-3b2317e8e532.tar.gz"}
PENDING_SHA = "PENDING-SHA256-FROM-RED"
PENDING_SIZE = "PENDING-SIZE"

# From the Ryzen's first RC2 install, which exited on Ubuntu's unattended-upgrades lock (1.2.6 preparation).
INSTALL_STATUS = ('<p class="notice" style="max-width:62rem;margin:.4rem 0 1rem">If the installer stops because Ubuntu is '
                  'still installing its own updates, wait a few minutes and run the install command again.</p>')
BLOCK = re.compile(r"<!--release:(?P<name>[a-z0-9-]+) (?P<attrs>[^>]*?)-->(?P<body>.*?)<!--/release:(?P=name)-->", re.S)

LICENCE_OLD = ("buy a one-time licence for your own PC in My subscription, from $49.99 for 8 cameras")
LICENCE_NEW = ("for your own PC, buy a one-time licence with your camera plan in "
               "<a href=\"build-your-system.html\" style=\"color:#16778b;text-decoration:underline;font-weight:700\">Build Your System</a>, "
               "from $49.99 for 8 cameras")


def mb(size) -> str:
    return f"{int(size) / 1_000_000:.1f} MB" if str(size).isdigit() else "about 900 MB"


def replace_once(html: str, old: str, new: str, what: str) -> str:
    count = html.count(old)
    if count != 1:
        raise SystemExit(f"ABORT: expected exactly one {what!r}, found {count}")
    return html.replace(old, new)


def release_blocks(html: str, new: dict) -> str:
    def one(match):
        body = match["body"]
        for key in ("url", "filename", "sha256"):
            body = body.replace(OLD[key], str(new[key]))
        body = body.replace(mb(OLD["size"]), mb(new["size"])).replace(OLD["version"], VERSION)
        attrs = match["attrs"]
        for key in ("url", "filename", "sha256"):
            attrs = attrs.replace(OLD[key], str(new[key]))
        attrs = attrs.replace(f"version={OLD['version']}", f"version={VERSION}").replace(f"size={OLD['size']}", f"size={new['size']}")
        attrs = attrs.replace("state=published", f"state={new['state']}")
        if match["name"] == "vms-card-status" and new["state"] != "published":
            # Until the approved host serves the verified installer, the release never reads as available.
            body = body.replace('<span class="pill">Available now</span>', '<span class="pill">Release pending</span>')
        if match["name"] == "linux-install-status" and not body.strip():
            body = INSTALL_STATUS
        return f"<!--release:{match['name']} {attrs}-->{body}<!--/release:{match['name']}-->"
    return BLOCK.sub(one, html)


SIGNUP_OLD = 'href="https://app.anyaicam.com/customer-register"'
# Self-service sign-up (direct_onboarding /customer-signup), not the installer-request page that waits for an
# administrator's approval (/customer-register). Found 2026-10-16 while preparing Requirement #11.
SIGNUP_NEW = 'href="https://app.anyaicam.com/customer-signup"'


def signup_link(html: str, page: str) -> str:
    return replace_once(html, SIGNUP_OLD, SIGNUP_NEW, f"{page} Create an account link")


def build_vms(html: str, new: dict) -> str:
    html = signup_link(release_blocks(html, new), "vms.html")
    # Linux card: accurate purchase path + free installer / paid licence and plan.
    html = replace_once(html, LICENCE_OLD, LICENCE_NEW, "vms.html licence sentence")
    html = replace_once(
        html,
        '<p style="font-size:.9rem;color:#64748b;margin-top:.7rem">Activating AnyAiCam needs an AnyAiCam account with a VMS licence.',
        '<p style="font-size:.9rem;color:#64748b;margin-top:.7rem">The installer is a free download: install it and explore '
        'AnyAiCam before activation. To run cameras, create your account, link this PC and choose <strong>Activate Your '
        'System</strong> to buy your monthly camera plan and the one-time VMS licence in one checkout.',
        "vms.html activation note")
    # Windows card: clearly a preview, not a released product; no licensing claims.
    html = replace_once(html, """        <span class="pill">Windows</span>
        <h3>AnyAiCam VMS for Windows 1.3.0</h3>
        <p>The current AnyAiCam VMS installer for a compatible Windows PC.</p>
        <div class="button-row" style="margin-top:.6rem">
          <a class="button primary" href="https://anyaicam.com/downloads/windows/1.3.0/AnyAiCam-VMS-Setup-1.3.0-aedecb6.exe">Download for Windows</a>
        </div>
        <p style="font-size:.9rem;color:#64748b;margin-top:.7rem">Version 1.3.0 · Windows · 577.4 MB</p>""",
                        """        <span class="pill future">Windows preview</span>
        <h3>AnyAiCam VMS for Windows 1.3.0 (preview)</h3>
        <p>An early preview of AnyAiCam VMS for a compatible Windows PC, for evaluation only. It is not a commercial release and does not yet include everything in the Linux version. For a production system, use AnyAiCam for Linux or an AnyAiCam appliance.</p>
        <div class="button-row" style="margin-top:.6rem">
          <a class="button light" href="https://anyaicam.com/downloads/windows/1.3.0/AnyAiCam-VMS-Setup-1.3.0-aedecb6.exe">Download the Windows preview</a>
        </div>
        <p style="font-size:.9rem;color:#64748b;margin-top:.7rem">Preview · Version 1.3.0 · Windows · 577.4 MB · free download, provided for evaluation without commercial support</p>""",
                        "vms.html Windows card")
    return html


def build_linux(html: str, new: dict) -> str:
    html = signup_link(release_blocks(html, new), "vms-linux.html")
    html = replace_once(html, '<p style="font-size:.9rem">Windows version coming soon.</p>',
                        '<p style="font-size:.9rem">Windows: a <a href="vms.html#windows-download">preview</a> is available '
                        'for evaluation only.</p>', "vms-linux Windows line")
    html = replace_once(html, LICENCE_OLD.replace("buy", "buy"), LICENCE_NEW, "vms-linux licence item")
    html = replace_once(html, "<li>An Ubuntu user that can run <code>sudo</code></li></ul></article>",
                        "<li>A monthly camera plan. Your licence and plan set how many cameras run, and AnyAiCam "
                        f"{VERSION} enforces that on this PC</li><li>An Ubuntu user that can run <code>sudo</code></li></ul></article>",
                        "vms-linux account card")
    # Unpack into a fresh, versioned folder (the archive has no top-level folder; Red, 2026-10-16).
    html = replace_once(html, f"mkdir -p ~/anyaicam-installer\ntar -xzf {FILENAME} -C ~/anyaicam-installer<!--/release:linux-commands-->\n"
                              "cd ~/anyaicam-installer\nsudo ./install.sh\nsudo ./validate.sh</pre>",
                        f"mkdir ~/AnyAiCam-{VERSION}\ntar -xzf {FILENAME} -C ~/AnyAiCam-{VERSION}<!--/release:linux-commands-->\n"
                        f"cd ~/AnyAiCam-{VERSION}\nsudo ./install.sh --product-mode=local\nsudo ./validate.sh</pre>",
                        "vms-linux commands")
    html = replace_once(html, "<li>Unpack it, run the installer, then run the built-in check:</li>",
                        "<li>Unpack it into a new folder, run the installer, then run the built-in check. If your plan is "
                        "Hybrid, use <code>--product-mode=hybrid</code> (once linked, AnyAiCam follows your plan). If the folder "
                        "already exists, choose a new folder name.</li>",
                        "vms-linux unpack step")
    html = replace_once(html, "<li>In the AnyAiCam portal, follow <strong>Set up your appliance</strong> to link this PC to your account and add your cameras.</li>",
                        "<li>Link this PC to your account: on the PC, open <strong>AnyAiCam Setup → Link this appliance</strong> "
                        "(or run <code>anyaicam-setup --claim</code> in a terminal) and follow the steps. A link code expires after "
                        "15 minutes. Then add your cameras from the AnyAiCam portal. A PC that is already linked keeps its link: "
                        "do not link it again.</li>", "vms-linux link step")
    html = replace_once(html, '<article class="card"><h3>Repair</h3>',
                        '<article class="card"><h3>Upgrading</h3><p>From 1.2.4, 1.2.5 or 1.2.6: download this release, unpack it '
                        'into a new folder and run <code>sudo ./install.sh</code> (or the same command you used before; reinstalling '
                        'cannot switch the product mode). Cameras, recordings, settings and the link to your account are kept, and a '
                        'rollback point is saved first. An older installer will not replace a newer version.</p></article>\n'
                        '      <article class="card"><h3>Repair</h3>', "vms-linux upgrade card")
    html = replace_once(html, "Recordings, cameras, settings and accounts are kept.</p></article>",
                        "Recordings, cameras, settings and accounts are kept. To go back to the version you had before the last "
                        "upgrade, run <code>sudo anyaicam-rollback</code>.</p></article>", "vms-linux repair card")
    return html


def main() -> None:
    global FILENAME
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename", default=FILENAME, help="installer file name from Red's verified build")
    parser.add_argument("--sha256", default=PENDING_SHA)
    parser.add_argument("--size", default=PENDING_SIZE)
    # Installer hosting: S3 + CloudFront (owner direction 2026-10-16), pages stay on Bluehost. The public host is
    # PENDING until the owner approves the hosting setup and its costs (see HOSTING-S3-CLOUDFRONT.md).
    # Option A (owner decision 2026-10-16): the existing website download path, as for 1.2.4.
    parser.add_argument("--url-base", default=f"https://anyaicam.com/downloads/linux/{VERSION}")
    # "published" only through finalize_download_host.sh, after the owner-approved host serves the verified installer.
    parser.add_argument("--state", choices=("pending", "published"), default="pending")
    args = parser.parse_args()
    FILENAME = args.filename
    assert re.fullmatch(r"anyaicam-appliance-installer-1\.2\.7-vms-[0-9a-f]{12}\.tar\.gz", FILENAME), FILENAME
    if args.state == "published" and ("PENDING" in args.url_base or "PENDING" in args.sha256 or "PENDING" in str(args.size)):
        raise SystemExit("ABORT: a release can be marked published only with the approved host and verified values")
    new = {"filename": FILENAME, "sha256": args.sha256, "size": args.size, "url": f"{args.url_base.rstrip('/')}/{FILENAME}",
           "state": args.state}
    out_dir = HERE / "prepared" / "public_html"
    out_dir.mkdir(parents=True, exist_ok=True)
    for page, build in (("vms.html", build_vms), ("vms-linux.html", build_linux)):
        raw = (BASES / page).read_bytes()
        assert hashlib.sha256(raw).hexdigest().startswith(BASE_SHA[page]), f"base changed: {page}"
        html = build(raw.decode("utf-8"), new)
        assert OLD["filename"] not in html and OLD["sha256"] not in html, f"1.2.4 left in {page}"
        (out_dir / page).write_bytes(html.encode("utf-8"))
        print(page, hashlib.sha256(html.encode("utf-8")).hexdigest(), "PENDING" if "PENDING-" in html else "complete")
    if args.sha256 != PENDING_SHA:
        (out_dir / f"{FILENAME}.sha256").write_text(f"{args.sha256}  {FILENAME}\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
