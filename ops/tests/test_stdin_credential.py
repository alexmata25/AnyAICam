"""Regression coverage for the confirmed-live CRLF stdin-credential bug:
every local operator helper this session that piped a secret from a
PowerShell hidden-input prompt over SSH into a remote Python process
read it with `sys.stdin.readline().rstrip('\n')`. PowerShell terminates
each piped line with \r\n, so that pattern silently left a trailing \r
on every captured value -- a camera username/password stored this way
was then (correctly) rejected by the physical camera as a credential
mismatch, and a portal password's stored hash was computed against the
same \r-suffixed value. Nothing about either failure was a wrong-
password or camera-permissions problem.

read_stdin_credential_line() (stdin_credential.py) fixes this by
stripping only a trailing \r and/or \n -- never a bare str.strip(),
since a credential may legitimately contain leading or trailing
spaces, which must survive exactly."""

import io
import unittest

from stdin_credential import read_stdin_credential_line


class ReadStdinCredentialLineTests(unittest.TestCase):
    def test_lf_only_terminator(self):
        self.assertEqual(read_stdin_credential_line(io.StringIO('hunter2\n')), 'hunter2')

    def test_crlf_windows_terminator_produces_the_exact_original_bytes(self):
        """The actual confirmed-live bug: PowerShell pipes password\r\n,
        not password\n -- the fix must strip both characters, not just
        the \n, leaving no trailing \r behind."""
        self.assertEqual(read_stdin_credential_line(io.StringIO('hunter2\r\n')), 'hunter2')

    def test_cr_only_terminator(self):
        self.assertEqual(read_stdin_credential_line(io.StringIO('hunter2\r')), 'hunter2')

    def test_no_trailing_newline_at_all(self):
        """e.g. the final line of a stream with no trailing terminator."""
        self.assertEqual(read_stdin_credential_line(io.StringIO('hunter2')), 'hunter2')

    def test_leading_and_trailing_spaces_are_preserved_exactly(self):
        """A credential may intentionally contain spaces -- this must
        never call str.strip(), only strip the trailing line
        terminator characters."""
        self.assertEqual(read_stdin_credential_line(io.StringIO('  My Pass Phrase  \r\n')), '  My Pass Phrase  ')

    def test_empty_line(self):
        self.assertEqual(read_stdin_credential_line(io.StringIO('\r\n')), '')

    def test_only_the_trailing_terminator_is_stripped_not_an_embedded_cr(self):
        """Proves this uses rstrip() (end-anchored) rather than a blanket
        character removal -- an embedded \r elsewhere in the value (however
        unlikely in a real credential) must survive untouched."""
        self.assertEqual(read_stdin_credential_line(io.StringIO('pass\rword\r\n')), 'pass\rword')

    def test_default_stream_is_sys_stdin(self):
        import sys
        from unittest.mock import patch
        with patch.object(sys, 'stdin', io.StringIO('hunter2\r\n')):
            self.assertEqual(read_stdin_credential_line(), 'hunter2')


if __name__ == '__main__':
    unittest.main()
