"""Windows 1.3.2 (Blue, Codex FAIL on ef93b503): Setup tells the customer the supported first-time activation rule.

Supported today (app/vms_capacity.py, unchanged): a PC runs 0 cameras until the cloud's first signed capacity arrives,
which needs ONE online link to an AnyAiCam account. The accepted capacity is then verified without an expiry check, so
Local cameras, recording and live view keep working without internet. There is no approved offline (air-gapped) first
activation (docs/windows-1.3.2-local-licensing-proposal.md is a proposal for the owner).

The wording must say exactly that: link once, online, before cameras start; offline afterwards. It must not promise
offline activation, and must not suggest the link is only for remote viewing (the 1.3.0-era finish text said "to see
this computer's cameras from anywhere, link it", which hid that no camera runs before the link).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
CODE = ISS.split("[Code]", 1)[1]
RUNTIME = (WIN / "install-runtime.ps1").read_text(encoding="utf-8")
FORBIDDEN = ("no internet needed", "no internet required", "fully offline", "air-gapped", "air gapped",
             "without an account", "without linking", "works offline out of the box", "offline activation")


def _pascal_strings(text: str) -> str:
    """Every single-quoted Pascal string literal, '' unescaped, concatenated as Pascal does: what the customer reads.
    Full-line // comments are dropped first (an apostrophe in a comment is not a string delimiter)."""
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    return "".join(s.replace("''", "'") for s in re.findall(r"'((?:[^']|'')*)'", code))


class LocalActivationWordingTests(unittest.TestCase):
    def mode_page(self):
        return _pascal_strings(CODE.split("procedure InitializeWizard;", 1)[1].split("\nend;", 1)[0])

    def finish_page(self):
        body = CODE.split("procedure CurPageChanged(CurPageID: Integer);", 1)[1].split("\nend;", 1)[0]
        linked, unlinked = body.split("else if HasClaimLink then", 1)
        with_link, without_link = unlinked.split("\n  else\n", 1)
        return _pascal_strings(linked), _pascal_strings(with_link), _pascal_strings(without_link)

    def test_the_mode_page_states_the_one_time_online_link_for_both_modes(self):
        page = self.mode_page()
        self.assertIn("Both modes need this computer linked to your AnyAiCam account once, with an internet connection, "
                      "before your cameras start.", page)
        self.assertIn("After that, Local mode keeps recording and live view working on this computer without internet.", page)
        self.assertIn("Local: cameras, recording and live view stay on this computer (link once online to activate)", page)

    def test_both_unlinked_finish_messages_say_cameras_start_after_the_first_link(self):
        _, with_link, without_link = self.finish_page()
        for text in (with_link, without_link):
            self.assertIn("Your cameras start once this computer is linked to your AnyAiCam account (needed once, with an "
                          "internet connection).", text)
            self.assertIn("After this first link, Local mode keeps recording without internet.", text)
            self.assertNotIn("from anywhere", text)          # the link is not only for remote viewing
        self.assertIn("Start menu > AnyAiCam > Link this computer to AnyAiCam (needs an administrator of this PC)", without_link)

    def test_the_linked_message_is_unchanged(self):
        linked, _, _ = self.finish_page()
        self.assertIn("This computer is linked to your AnyAiCam account.", linked)

    def test_the_setup_log_says_the_same(self):
        self.assertIn("which this computer receives once it is linked to an AnyAiCam account (needed once, online). "
                      "After that, Local cameras keep working without internet.", RUNTIME)

    def test_nothing_customer_facing_promises_offline_activation(self):
        readable = (self.mode_page() + " " + " ".join(self.finish_page()) + " " +
                    " ".join(re.findall(r"Write-Output '((?:[^']|'')*)'", RUNTIME))).lower()
        for phrase in FORBIDDEN:
            self.assertNotIn(phrase, readable)


class WordingMatchesTheCodeTests(unittest.TestCase):
    """The wording is only true while the capacity code behaves this way; a change there must revisit the wording."""
    def test_an_accepted_capacity_is_not_expired_offline(self):
        source = (ROOT / "app" / "vms_capacity.py").read_text(encoding="utf-8")
        accepted = source.split("def _accepted_signed()", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_verified_capacity(snapshot, check_expiry=False, public_keys=accepted_keys)", accepted)

    def test_no_capacity_means_no_cameras(self):
        source = (ROOT / "app" / "vms_capacity.py").read_text(encoding="utf-8")
        licensed = source.split("def licensed_camera_numbers(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if capacity is None:\n        return []", licensed)


if __name__ == "__main__":
    unittest.main()
