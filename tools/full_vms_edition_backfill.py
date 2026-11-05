"""Operator CLI for the audited Full VMS edition backfill (app/full_vms_edition_backfill.py).

  readiness                        read-only: appliances by recorded edition
  plan --created-before TS --out F read-only: write the review plan and print its SHA-256
  apply --plan F --sha256 H --actor-email E --i-am-authorized
                                   designate the plan's eligible appliances (one transaction)

The database is whatever ANYAICAM_PARTNER_DB / ANYAICAM_DATABASE_URL select.
apply changes that database: run it only under a separate production
authorization, after a person has reviewed the exact plan file.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("readiness")
    plan = commands.add_parser("plan")
    plan.add_argument("--created-before", required=True)
    plan.add_argument("--out", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--sha256", required=True)
    apply.add_argument("--actor-email", required=True)
    apply.add_argument("--i-am-authorized", action="store_true")
    args = parser.parse_args(argv)

    import full_vms_edition_backfill as backfill
    from partner_db import connection, transaction
    if args.command == "readiness":
        with connection() as db:
            print(json.dumps(backfill.readiness(db), indent=2, sort_keys=True))
        return 0
    if args.command == "plan":
        out = Path(args.out)
        if out.exists():
            print(f"Refusing to overwrite {out}", file=sys.stderr)
            return 2
        with connection() as db:
            result, digest = backfill.build_plan(db, created_before=args.created_before)
        out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"eligible={len(result['eligible'])} review={len(result['review'])} sha256={digest}")
        return 0
    if not args.i_am_authorized:
        print("apply changes the database; pass --i-am-authorized only under a separate authorization.", file=sys.stderr)
        return 2
    reviewed = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    try:
        with transaction() as db:
            count = backfill.apply_plan(db, reviewed, args.sha256,
                                        actor={"email": args.actor_email, "role": "administrator"})
    except backfill.BackfillRefused as error:
        print(f"Refused; nothing was changed: {error}", file=sys.stderr)
        return 1
    print(f"designated full_vms: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
