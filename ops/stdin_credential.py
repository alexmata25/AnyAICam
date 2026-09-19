"""Reads exactly one credential value from stdin for local operator
tooling that pipes a secret from a PowerShell hidden-input prompt over
SSH into a remote Python process (see docs/blockers-before-universal-
release.md and this session's provisioning helper scripts).

Windows PowerShell's pipeline-to-native-process conversion terminates
each piped string with \r\n (the platform's own line ending), not a
bare \n. A naive `sys.stdin.readline().rstrip('\n')` strips only the
\n, leaving a stray trailing \r baked into the value -- confirmed live
against a real Samsung appliance: a camera username/password captured
this way silently gained a trailing \r, which the physical camera then
(correctly) rejected as a credential mismatch, and a portal password's
stored hash was computed against the same \r-suffixed value. None of
that was a wrong-password or camera-permissions problem; it was this
exact stripping bug, present identically in every stdin-piping helper
written this session.

read_stdin_credential_line() strips a trailing \r and/or \n --
whichever is actually present, in either order, without requiring
both -- and nothing else. It deliberately never calls str.strip():
a credential may legitimately contain leading or trailing spaces, and
those must never be discarded.
"""

import sys
from typing import TextIO


def read_stdin_credential_line(stream: TextIO | None = None) -> str:
    """Reads one line from stream (default sys.stdin) and strips only a
    trailing \\r and/or \\n. Preserves every other character exactly,
    including intentional leading/trailing spaces and any character
    that isn't itself a trailing \\r or \\n."""
    stream = sys.stdin if stream is None else stream
    return stream.readline().rstrip('\r\n')
