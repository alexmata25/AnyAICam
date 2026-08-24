"""Session-wide autouse reset for recording_uploader's module-level
caches (backlog cutoff, quarantine, and persisted-uploaded-file state)
between every test, regardless of whether an individual test file's
own fixture already resets some of these.

These caches are loaded once per process lifetime by design (see each
one's own docstring in recording_uploader.py) to match real production
behavior -- a real worker process only ever needs to read its on-disk
state file once and keep it current in memory afterward. But that same
design means that, without this reset, one test's cache state (e.g. a
filename recorded via _record_successful_upload()) can leak into a
completely unrelated later test that happens to run in the same
pytest process and reuse a similar filename. This conftest exists
specifically because _already_uploaded_for_camera() -- unlike
quarantine, which only matters on a failure path -- is consulted on
every single _relay_camera_once() call, so any test exercising the
normal success path is affected.

This complements, not replaces, any individual test file's own more
specific isolation (e.g. monkeypatching RECORDINGS_FOLDER/CUTOFF_FILE/
QUARANTINE_FILE/UPLOADED_FILE to a tmp_path)."""
import pytest

import recording_uploader as ru


@pytest.fixture(autouse=True)
def _reset_recording_uploader_module_caches():
    ru._cutoff_cache = None
    ru._quarantine_cache = None
    ru._uploaded_state_cache = None
    ru._uploaded_files.clear()
    yield
    ru._cutoff_cache = None
    ru._quarantine_cache = None
    ru._uploaded_state_cache = None
    ru._uploaded_files.clear()
