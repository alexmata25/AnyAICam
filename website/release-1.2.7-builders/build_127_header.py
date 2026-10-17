"""header-standard.css (shared by vms, vms-linux, plans, Build Your System and the other stage-header pages): on phones the
12-link navigation becomes one swipeable row instead of a ~300 px block above every page. CSS only; desktop unchanged.
DRAFT. Base = the file anyaicam.com serves (2026-10-16); re-check against the server before publishing."""
import hashlib
from pathlib import Path

HERE = Path(__file__).parent
raw = (HERE / "base" / "header-standard.css").read_bytes()
BASE_SHA = hashlib.sha256(raw).hexdigest()
ADD = """

/* Phones (2026-10-16, VMS 1.2.7 UI pass): one swipeable row of links instead of a block that fills the first screen. */
@media (max-width: 640px) {
  .stage-header .stage-nav {
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
    overscroll-behavior-x: contain;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    padding-bottom: .15rem !important;
    -webkit-mask-image: linear-gradient(to right, #000 88%, transparent);
    mask-image: linear-gradient(to right, #000 88%, transparent);
  }
  .stage-header .stage-nav::-webkit-scrollbar { display: none; }
  .stage-header .stage-nav a {
    flex: 0 0 auto !important;
    white-space: nowrap !important;
    padding: .6rem .7rem !important;
  }
}
"""
out = raw.decode("utf-8").rstrip("\n") + ADD
(HERE / "prepared" / "public_html" / "header-standard.css").write_bytes(out.encode("utf-8"))
print("base", BASE_SHA, "-> prepared", hashlib.sha256(out.encode()).hexdigest())
