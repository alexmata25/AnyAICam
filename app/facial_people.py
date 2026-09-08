"""AAC enrollment/watchlist DB service -- Phase 1.

Every function here takes an explicit `db` connection (matching
camera_mapping.py/customer_analytics_panel.py's established
dependency-light pattern in this codebase) and an explicit
`customer_id`, and every query is scoped by that customer_id -- there
is no function anywhere in this module that can read or write a
facial_people/facial_embeddings/facial_watchlists row belonging to a
different customer, even if a caller passes a person_id/watchlist_id
that exists under another tenant. That is the one property this
module's own test suite (test_facial_people.py) checks most
aggressively: a person_id or watchlist_id that is real, but belongs to
a different customer_id, must be treated identically to one that
doesn't exist at all (None/False/empty list), never as a 404 that
leaks "yes, that id exists").

This module never calls partner_db.audit() itself -- the calling route
(facial_recognition_ui.py) does that after each mutating call succeeds,
matching partner_portal.py's own established convention of routes
calling audit() directly rather than burying it inside a service
function.

Image retention: enroll_person()/add_reference_image() never persist
the original uploaded photo -- only the single aligned face crop
actually used to produce the embedding (via
facial_recognition.detect_and_embed()) is ever written to disk, under
AAC_FACES_FOLDER. The caller (the enrollment route) is responsible for
never writing the original upload to disk in the first place; this
module only ever accepts an already-produced embedding + an optional
already-cropped image path, so it cannot itself be the place a raw
upload leaks into permanent storage.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

AAC_FACES_FOLDER = Path(os.environ.get("ANYAICAM_AAC_FACES_FOLDER", "/app/recordings/aac_faces"))

VALID_WATCHLIST_CLASSIFICATIONS = frozenset({"alert", "allow"})
VALID_PERSON_STATUSES = frozenset({"active", "disabled"})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


def enroll_person(
    db,
    *,
    customer_id: str,
    display_name: str,
    site_id: str | None = None,
    external_reference: str | None = None,
    notes: str | None = None,
    created_by: str | None = None,
    now: str,
) -> str:
    """Creates a new facial_people row. Does not enroll any reference
    image/embedding on its own -- call add_reference_image() separately
    for each image (enrollment supports multiple reference images per
    person, added independently, so a partial failure part-way through
    a multi-image upload never corrupts the person record itself)."""
    display_name = str(display_name or "").strip()
    if not display_name:
        raise ValueError("display_name is required.")
    if not customer_id:
        raise ValueError("customer_id is required.")
    person_id = _new_id("person")
    db.execute(
        "INSERT INTO facial_people(id,customer_id,site_id,external_reference,display_name,status,notes,created_at,updated_at,created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (person_id, customer_id, site_id, external_reference, display_name, "active", notes, now, now, created_by),
    )
    return person_id


def get_person(db, *, customer_id: str, person_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM facial_people WHERE id=? AND customer_id=?",
        (person_id, customer_id),
    ).fetchone()
    return dict(row) if row else None


def list_people(
    db,
    *,
    customer_id: str,
    site_id: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM facial_people WHERE customer_id=?"
    params: list = [customer_id]
    if site_id:
        query += " AND site_id=?"
        params.append(site_id)
    if status:
        query += " AND status=?"
        params.append(status)
    if search:
        query += " AND (display_name LIKE ? OR external_reference LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    query += " ORDER BY display_name"
    return [dict(row) for row in db.execute(query, tuple(params)).fetchall()]


def update_person(
    db,
    *,
    customer_id: str,
    person_id: str,
    display_name: str | None = None,
    external_reference: str | None = None,
    notes: str | None = None,
    status: str | None = None,
    now: str,
) -> bool:
    if get_person(db, customer_id=customer_id, person_id=person_id) is None:
        return False
    if status is not None and status not in VALID_PERSON_STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    fields = {"display_name": display_name, "external_reference": external_reference, "notes": notes, "status": status}
    assignments = [f"{name}=?" for name, value in fields.items() if value is not None]
    values = [value for value in fields.values() if value is not None]
    if not assignments:
        return True
    assignments.append("updated_at=?")
    values.append(now)
    values.extend([person_id, customer_id])
    db.execute(f"UPDATE facial_people SET {','.join(assignments)} WHERE id=? AND customer_id=?", tuple(values))
    return True


def _embedding_image_paths(db, *, customer_id: str, person_id: str) -> list[str]:
    rows = db.execute(
        "SELECT source_image_path FROM facial_embeddings WHERE person_id=? AND customer_id=?",
        (person_id, customer_id),
    ).fetchall()
    return [row["source_image_path"] for row in rows if row["source_image_path"]]


def delete_person(db, *, customer_id: str, person_id: str) -> bool:
    """Hard-deletes the person, every one of their facial_embeddings
    rows (the actual biometric templates), every reference-image file
    on disk, and every facial_watchlist_members row for them (via the
    schema's own ON DELETE CASCADE for embeddings/memberships).

    Deliberately does NOT touch facial_events: past match history keeps
    its own denormalized matched_person_name/matched_watchlist_name
    snapshot (see the migration's own comment) and its
    matched_person_id simply stops resolving to a live person -- event/
    audit integrity is preserved exactly as the Phase 1 spec requires,
    while every biometric template is genuinely gone, not merely
    hidden.

    Returns False (no-op) if the person doesn't exist under this
    customer_id -- never raises, so a caller retrying a delete that
    already succeeded gets a clean, idempotent False rather than an
    error."""
    if get_person(db, customer_id=customer_id, person_id=person_id) is None:
        return False
    image_paths = _embedding_image_paths(db, customer_id=customer_id, person_id=person_id)
    db.execute("DELETE FROM facial_people WHERE id=? AND customer_id=?", (person_id, customer_id))
    for path in image_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass  # Best-effort: the DB row (the actual template) is already gone either way.
    return True


# --------------------------------------------------------------------------
# Reference images / embeddings
# --------------------------------------------------------------------------


def add_reference_image(
    db,
    *,
    customer_id: str,
    person_id: str,
    embedding: tuple[float, ...],
    engine: str,
    engine_version: str,
    source_image_path: str | None = None,
    quality: float | None = None,
    now: str,
) -> str | None:
    """Returns the new embedding id, or None if person_id doesn't exist
    under customer_id (the tenant check that prevents attaching a
    reference image to another customer's person record even if the
    caller somehow has a valid person_id from elsewhere)."""
    if get_person(db, customer_id=customer_id, person_id=person_id) is None:
        return None
    embedding_id = _new_id("emb")
    db.execute(
        "INSERT INTO facial_embeddings(id,person_id,customer_id,engine,engine_version,embedding_json,source_image_path,quality,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (embedding_id, person_id, customer_id, engine, engine_version, json.dumps(list(embedding)), source_image_path, quality, now),
    )
    return embedding_id


def list_reference_images(db, *, customer_id: str, person_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT id,engine,engine_version,source_image_path,quality,created_at FROM facial_embeddings "
        "WHERE person_id=? AND customer_id=? ORDER BY created_at",
        (person_id, customer_id),
    ).fetchall()
    return [dict(row) for row in rows]


def delete_reference_image(db, *, customer_id: str, person_id: str, embedding_id: str) -> bool:
    row = db.execute(
        "SELECT source_image_path FROM facial_embeddings WHERE id=? AND person_id=? AND customer_id=?",
        (embedding_id, person_id, customer_id),
    ).fetchone()
    if row is None:
        return False
    db.execute("DELETE FROM facial_embeddings WHERE id=? AND customer_id=?", (embedding_id, customer_id))
    if row["source_image_path"]:
        try:
            Path(row["source_image_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return True


def enrolled_embeddings_for_matching(db, *, customer_id: str, engine: str, engine_version: str):
    """Every active person's embeddings for this customer, in the shape
    facial_recognition.match_face() consumes. Never returns embeddings
    for a disabled person (status != 'active') or for any other
    customer_id -- this is the query facial_events.py's in-memory match
    cache refreshes from, and is the one point that must never leak
    another tenant's biometric data into a live matching pass.

    Also scoped by engine_version, not just engine: a stored embedding
    from an older version of the same engine formula (e.g. before
    embed_face_crop()'s mean-centering was added) is excluded here, at
    the query itself, on top of match_face()'s own independent
    engine_version check -- an enrolled person whose only reference
    images predate an embedding-format change simply has no usable
    embeddings until re-enrolled, rather than being silently compared
    against a query embedding from an incompatible formula."""
    from facial_recognition import EnrolledEmbedding

    rows = db.execute(
        "SELECT fe.person_id AS person_id, fe.embedding_json AS embedding_json "
        "FROM facial_embeddings fe JOIN facial_people fp ON fp.id=fe.person_id "
        "WHERE fe.customer_id=? AND fe.engine=? AND fe.engine_version=? AND fp.customer_id=? AND fp.status='active'",
        (customer_id, engine, engine_version, customer_id),
    ).fetchall()
    return [
        EnrolledEmbedding(
            person_id=row["person_id"],
            embedding=tuple(json.loads(row["embedding_json"])),
            engine=engine,
            engine_version=engine_version,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------
# Watchlists
# --------------------------------------------------------------------------


def create_watchlist(
    db,
    *,
    customer_id: str,
    name: str,
    site_id: str | None = None,
    classification: str = "alert",
    description: str | None = None,
    created_by: str | None = None,
    now: str,
) -> str:
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required.")
    if classification not in VALID_WATCHLIST_CLASSIFICATIONS:
        raise ValueError(f"Unknown classification: {classification!r}")
    watchlist_id = _new_id("watchlist")
    db.execute(
        "INSERT INTO facial_watchlists(id,customer_id,site_id,name,classification,description,created_at,updated_at,created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (watchlist_id, customer_id, site_id, name, classification, description, now, now, created_by),
    )
    return watchlist_id


def get_watchlist(db, *, customer_id: str, watchlist_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM facial_watchlists WHERE id=? AND customer_id=?", (watchlist_id, customer_id)
    ).fetchone()
    return dict(row) if row else None


def list_watchlists(db, *, customer_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM facial_watchlists WHERE customer_id=? ORDER BY name", (customer_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def delete_watchlist(db, *, customer_id: str, watchlist_id: str) -> bool:
    """Deletes the watchlist and its memberships (ON DELETE CASCADE).
    Never deletes the member people themselves -- only their membership
    in this one list."""
    if get_watchlist(db, customer_id=customer_id, watchlist_id=watchlist_id) is None:
        return False
    db.execute("DELETE FROM facial_watchlists WHERE id=? AND customer_id=?", (watchlist_id, customer_id))
    return True


def add_watchlist_member(db, *, customer_id: str, watchlist_id: str, person_id: str, added_by: str | None = None, now: str) -> bool:
    """Both watchlist_id and person_id must belong to customer_id --
    checked independently, so neither a foreign watchlist nor a foreign
    person can ever be linked together, even across two different
    other tenants."""
    if get_watchlist(db, customer_id=customer_id, watchlist_id=watchlist_id) is None:
        return False
    if get_person(db, customer_id=customer_id, person_id=person_id) is None:
        return False
    db.execute(
        # database_backend._postgres_sql() rewrites "INSERT OR IGNORE" into
        # "INSERT ... ON CONFLICT DO NOTHING" for the Postgres backend --
        # same idiom used throughout this codebase (e.g. camera_credentials
        # upserts), never a raw dialect-specific statement here.
        "INSERT OR IGNORE INTO facial_watchlist_members(watchlist_id,person_id,added_at,added_by) VALUES(?,?,?,?)",
        (watchlist_id, person_id, now, added_by),
    )
    return True


def remove_watchlist_member(db, *, customer_id: str, watchlist_id: str, person_id: str) -> bool:
    if get_watchlist(db, customer_id=customer_id, watchlist_id=watchlist_id) is None:
        return False
    db.execute(
        "DELETE FROM facial_watchlist_members WHERE watchlist_id=? AND person_id=?", (watchlist_id, person_id)
    )
    return True


def list_watchlist_members(db, *, customer_id: str, watchlist_id: str) -> list[dict]:
    if get_watchlist(db, customer_id=customer_id, watchlist_id=watchlist_id) is None:
        return []
    rows = db.execute(
        "SELECT fp.id,fp.display_name,fp.external_reference,fp.status,fwm.added_at "
        "FROM facial_watchlist_members fwm JOIN facial_people fp ON fp.id=fwm.person_id "
        "WHERE fwm.watchlist_id=? AND fp.customer_id=? ORDER BY fp.display_name",
        (watchlist_id, customer_id),
    ).fetchall()
    return [dict(row) for row in rows]


def watchlisted_person_ids(db, *, customer_id: str) -> frozenset[str]:
    """Every person_id, for this customer, that belongs to at least one
    watchlist -- the set facial_recognition.classify_match() consumes
    to decide 'known' vs 'watchlist'."""
    rows = db.execute(
        "SELECT DISTINCT fwm.person_id FROM facial_watchlist_members fwm "
        "JOIN facial_people fp ON fp.id=fwm.person_id WHERE fp.customer_id=?",
        (customer_id,),
    ).fetchall()
    return frozenset(row["person_id"] for row in rows)


def person_watchlist_memberships(db, *, customer_id: str, person_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT fw.id,fw.name,fw.classification FROM facial_watchlist_members fwm "
        "JOIN facial_watchlists fw ON fw.id=fwm.watchlist_id "
        "WHERE fwm.person_id=? AND fw.customer_id=? ORDER BY fw.name",
        (person_id, customer_id),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "min_confidence": 0.6,
    "unknown_person_events_enabled": True,
    "debounce_seconds": 30,
    "engine": "haar_intensity",
}


def get_settings(db, *, customer_id: str) -> dict:
    """Returns this customer's AAC settings, or DEFAULT_SETTINGS
    (unwritten, not persisted) if they have never saved any -- matches
    this codebase's own no-hidden-default convention: a customer that
    never configured AAC gets the documented, code-level default, never
    a silently-different one from an empty row."""
    row = db.execute("SELECT * FROM facial_settings WHERE customer_id=?", (customer_id,)).fetchone()
    if row is None:
        return {"customer_id": customer_id, **DEFAULT_SETTINGS}
    result = dict(row)
    result["unknown_person_events_enabled"] = bool(result["unknown_person_events_enabled"])
    return result


def update_settings(db, *, customer_id: str, now: str, **fields) -> dict:
    current = get_settings(db, customer_id=customer_id)
    merged = {**{k: v for k, v in current.items() if k in DEFAULT_SETTINGS}, **{k: v for k, v in fields.items() if v is not None}}
    existing = db.execute("SELECT id FROM facial_settings WHERE customer_id=?", (customer_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE facial_settings SET min_confidence=?,unknown_person_events_enabled=?,debounce_seconds=?,engine=?,updated_at=? WHERE customer_id=?",
            (
                merged["min_confidence"],
                int(bool(merged["unknown_person_events_enabled"])),
                merged["debounce_seconds"],
                merged["engine"],
                now,
                customer_id,
            ),
        )
    else:
        db.execute(
            "INSERT INTO facial_settings(id,customer_id,min_confidence,unknown_person_events_enabled,debounce_seconds,engine,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                _new_id("settings"),
                customer_id,
                merged["min_confidence"],
                int(bool(merged["unknown_person_events_enabled"])),
                merged["debounce_seconds"],
                merged["engine"],
                now,
            ),
        )
    return get_settings(db, customer_id=customer_id)
