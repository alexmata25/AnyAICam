"""Phase 6a (docs/AI_HANDOFF.md Sec 8): resolves the mapping between a cloud
`cameras.id` (TEXT, multi-tenant) and the appliance-local `camera_number`
(INTEGER, 1..256) that app/main.py's FFmpeg/HLS pipeline actually uses.

Deliberately dependency-light: both public functions take an already-open
`db` connection (matching the calling convention already used throughout
partner_db.py/appliance_cloud.py -- `with connection() as db: ...`) rather
than importing partner_db/connection() themselves. This keeps this module,
and its own unit tests, free of the documented test-discovery-order fragility
that comes from importing `main` (see test_live_view_page_characterization.py
and test_scan_results_compatibility_wiring.py's docstrings for the same
pre-existing issue in this codebase).

The mapping is never inferred -- no IP matching, no discovery-id parsing, no
name/ordering heuristics. It is an explicit, human-confirmed assignment made
through the existing customer-setup wizard (see partner_workspace.py). An
unassigned camera_number (NULL) is a valid, safe, default state: any future
relay-start logic must fail closed on it rather than guess a value. A
successfully assigned camera_number also does not by itself prove that slot
is actually configured on the appliance today -- that is a separate,
appliance-side fact this module has no way to observe, and callers must not
treat a non-NULL camera_number as proof the slot exists.
"""

MIN_CAMERA_NUMBER = 1
MAX_CAMERA_NUMBER = 256


class CameraNumberConflict(Exception):
    """Raised by assign_camera_number() when the requested camera_number is
    already assigned to a different camera on the same appliance."""


def _validate_camera_number_value(camera_number):
    if camera_number is None:
        return None
    if isinstance(camera_number, bool):
        raise ValueError("camera_number must be an integer, not a boolean.")
    if not isinstance(camera_number, int):
        raise ValueError("camera_number must be an integer.")
    if not (MIN_CAMERA_NUMBER <= camera_number <= MAX_CAMERA_NUMBER):
        raise ValueError(
            f"camera_number must be between {MIN_CAMERA_NUMBER} and {MAX_CAMERA_NUMBER}."
        )
    return camera_number


def resolve_camera_number(db, camera_id: str, appliance_id: str, customer_id: str):
    """Return the assigned camera_number for camera_id, scoped to an expected
    appliance_id and customer_id -- or None if the camera doesn't exist,
    belongs to a different appliance, belongs to a different customer, or is
    genuinely unassigned. These four cases are deliberately indistinguishable
    to the caller so this function can never be used to probe whether some
    other tenant's camera_id exists.
    """
    row = db.execute(
        'SELECT camera_number FROM cameras WHERE id=? AND appliance_id=? AND customer_id=?',
        (camera_id, appliance_id, customer_id),
    ).fetchone()
    if not row:
        return None
    return row['camera_number']


def assign_camera_number(db, camera_id: str, camera_number, *, appliance_id: str, customer_id: str) -> None:
    """Assign (camera_number is an int in [1, 256]) or clear (camera_number is
    None) the appliance-local slot number for a cloud camera_id, scoped to an
    expected appliance_id/customer_id.

    Callers must pass appliance_id/customer_id obtained from a trusted,
    already-authorized source (e.g. a prior SELECT scoped to the
    authenticated caller's own customer_id) -- never from unvalidated request
    input -- since this function's own ownership check depends on them being
    correct.

    Raises:
        ValueError: camera_number is out of range/wrong type, or the camera
            has no appliance_id at all (a camera_number is meaningless
            without a specific appliance to be local to).
        LookupError: no camera row matches camera_id AND appliance_id AND
            customer_id all at once. One message covers "doesn't exist",
            "wrong appliance", and "wrong customer" alike -- same
            non-enumeration reasoning as resolve_camera_number().
        CameraNumberConflict: another camera on the same appliance_id already
            has this camera_number. The database's own partial unique index
            (app/db_migrations.py) is the authoritative backstop against a
            race between two concurrent assignments; this check exists so a
            normal conflict is a clean, catchable exception instead of a raw
            integrity error.
    """
    validated = _validate_camera_number_value(camera_number)

    camera = db.execute(
        'SELECT appliance_id, customer_id FROM cameras WHERE id=?',
        (camera_id,),
    ).fetchone()
    # Compared in Python, not in the WHERE clause: SQL "=" never matches NULL
    # to NULL, which would wrongly turn "camera has no appliance yet" into
    # "camera not found" if compared via `AND appliance_id=?` instead. This
    # form still fails closed for every mismatch, and only reaches the more
    # specific "no appliance assigned" error below once ownership is confirmed.
    if not camera or camera['customer_id'] != customer_id or camera['appliance_id'] != appliance_id:
        raise LookupError("Camera not found for this appliance and customer.")
    if not camera['appliance_id']:
        raise ValueError("Camera has no appliance assigned; camera_number cannot be set.")

    if validated is not None:
        conflict = db.execute(
            'SELECT id FROM cameras WHERE appliance_id=? AND camera_number=? AND id!=?',
            (appliance_id, validated, camera_id),
        ).fetchone()
        if conflict:
            raise CameraNumberConflict(
                f"camera_number {validated} is already assigned to another camera on this appliance."
            )

    db.execute('UPDATE cameras SET camera_number=? WHERE id=?', (validated, camera_id))
