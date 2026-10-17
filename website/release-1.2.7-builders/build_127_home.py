"""Homepage for 1.2.7 (DRAFT): Windows preview wording and the hidden Linux release marker. Base = index.html as
published 2026-10-16 (homepage comparison, sha256 0f8fd708...), which the live page matches apart from
Cloudflare's email rewriting. Exact single-occurrence replacements only; no visible version number is added."""
import difflib
import hashlib
import re
from pathlib import Path

HERE = Path(__file__).parent
raw = (HERE / "base" / "index.html").read_bytes()
assert hashlib.sha256(raw).hexdigest().startswith("0f8fd708b7711e8a"), "base changed"
html = raw.decode("utf-8")
import argparse
_args = argparse.ArgumentParser()
_args.add_argument("--filename", required=True)
_args.add_argument("--sha256", required=True)
_args.add_argument("--size", type=int, required=True)
_args.add_argument("--url-base", default="https://anyaicam.com/downloads/linux/1.2.7")  # Option A
_args.add_argument("--state", choices=("pending", "published"), default="pending")
ARGS = _args.parse_args()
SHA, SIZE, FILE = ARGS.sha256, ARGS.size, ARGS.filename
marker = re.search(r"<!--release:home-linux-note ([^>]*)-->", html)
assert marker and html.count("<!--release:home-linux-note ") == 1
attrs = marker.group(1)
new_attrs = re.sub(r"version=\S+", "version=1.2.7", attrs)
new_attrs = re.sub(r"filename=\S+", f"filename={FILE}", new_attrs)
new_attrs = re.sub(r"sha256=\S+", f"sha256={SHA}", new_attrs)
new_attrs = re.sub(r"url=\S+", f"url={ARGS.url_base.rstrip('/')}/{FILE}", new_attrs)
new_attrs = re.sub(r"size=\S+", f"size={SIZE}", new_attrs)
if ARGS.state == "published" and "PENDING" in ARGS.url_base:
    raise SystemExit("ABORT: published needs the approved host")
new_attrs = re.sub(r"state=\S+", f"state={ARGS.state}", new_attrs)
html = html.replace(marker.group(0), f"<!--release:home-linux-note {new_attrs}-->", 1)
old = "<!--/release:home-linux-note--> Windows version coming soon.</p>"
assert html.count(old) == 1
html = html.replace(old, '<!--/release:home-linux-note--> A <a href="vms.html#windows-download">Windows preview</a> is available for evaluation.</p>')
out = HERE / "prepared" / "public_html" / "index.html"
out.write_bytes(html.encode("utf-8"))
visible = lambda s: re.sub(r"<!--.*?-->", "", s, flags=re.S)
changed = [l for l in difflib.unified_diff(visible(raw.decode()).split("\n"), visible(html).split("\n"), lineterm="", n=0)
           if l[:1] in "+-" and not l.startswith(("+++", "---"))]
assert len(changed) == 2 and "Windows preview" in changed[1], changed
print("index.html", hashlib.sha256(html.encode()).hexdigest(), "- visible change:", changed[1].strip()[-120:])
