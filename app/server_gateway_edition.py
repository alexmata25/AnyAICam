"""Server-authorized edition selection for newly provisioned appliances."""

# appliances.gateway_edition values. NULL means the server holds no trusted
# edition record; cloud media is never issued on the assumption of Full VMS.
FULL_VMS = "full_vms"
EDITIONS = (FULL_VMS, "local_gateway", "cloud_gateway")

# Every server table that records Gateway involvement for an appliance, with
# the edition it implies: a column name, a fixed edition, or None when it
# proves a Gateway without saying which one.
GATEWAY_EVIDENCE = (
    ("appliance_edition_provisioning_decisions", "column:edition"),
    ("internal_gateway_provisioning_authorizations", "column:edition"),
    ("gateway_checkout_attempts", "column:edition"),
    ("local_gateway_remote_subscriptions", "local_gateway"),
    ("cloud_gateway_camera_entitlements", "cloud_gateway"),
    ("gateway_stable_installation_claim_ids", None),
    ("gateway_claim_recovery_requests", None),
)


class EditionUnverifiable(LookupError):
    """The schema cannot establish an appliance's edition."""


def _table_exists(db, name):
    from database_backend import backend
    if backend() == "sqlite":
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    return db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?",
                      (name,)).fetchone() is not None


def edition_records(db, appliance_id):
    """(recorded marker, Gateway editions in evidence, any Gateway evidence).

    Reads only server records, current or historical. Raises
    EditionUnverifiable when the marker column or any evidence table is
    missing, so an unmigrated schema never looks like Full VMS.
    """
    appliance = db.execute("SELECT * FROM appliances WHERE id=?", (appliance_id,)).fetchone()
    if appliance is None:
        raise EditionUnverifiable("Appliance not found.")
    appliance = dict(appliance)
    if "gateway_edition" not in appliance:
        raise EditionUnverifiable("Appliance edition column is missing.")
    editions, any_gateway = set(), False
    for table, implies in GATEWAY_EVIDENCE:
        if not _table_exists(db, table):
            raise EditionUnverifiable(f"Gateway evidence table is missing: {table}")
        if implies and implies.startswith("column:"):
            found = [item[implies[7:]] for item in db.execute(
                f"SELECT {implies[7:]} FROM {table} WHERE appliance_id=?", (appliance_id,)).fetchall()]
            editions.update(found)
        else:
            found = db.execute(f"SELECT 1 FROM {table} WHERE appliance_id=? LIMIT 1", (appliance_id,)).fetchall()
            if found and implies:
                editions.add(implies)
        any_gateway = any_gateway or bool(found)
    return appliance["gateway_edition"], editions, any_gateway


def onboarding_edition(payload, *, is_global_admin):
    """The edition recorded for an appliance a server-side order creates.

    Portal onboarding and self-service provisioning create Full VMS orders, so
    they record full_vms explicitly. Only a live-authorized global
    administrator can choose Local Gateway instead.
    """
    edition = payload.get("gateway_edition")
    if edition is None or edition == FULL_VMS:
        return FULL_VMS
    if is_global_admin is not True:
        raise PermissionError("Gateway edition requires a global administrator.")
    if edition != "local_gateway":
        raise ValueError("Unsupported Gateway edition.")
    return edition
