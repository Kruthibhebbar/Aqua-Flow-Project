"""
Find and safely resolve duplicate cash-deposit (cod_collection) ledger rows.

Before the fix, one driver deposit produced TWO ledger rows: a stale
"Waiting Admin Approval" row from the submission plus a second "Settled"
(shown as "Verified") row from the approval. This tool pairs them and retires the stale one.

  python tools/resolve_duplicate_cod_ledger.py            # DRY RUN: only reports
  python tools/resolve_duplicate_cod_ledger.py --apply    # retire the stale rows

NOTHING IS DELETED. The stale row is marked status="Superseded" with a pointer to
the row that was kept; the submission reference/screenshot it carried is copied
onto the kept row. Superseded rows disappear from Recent Financial Activity and are
never counted in totals (totals only ever count "Settled"), but remain in the database
for audit. Rows are paired only within the SAME booking, driver and amount, so
separate deposits that merely share an amount are never touched. Safe to re-run.
Back up the payment_transactions collection first (e.g. mongodump) before --apply.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payment_ledger import find_duplicate_cod_rows, supersede_duplicate_cod_rows  # noqa: E402


def main(argv=None, coll=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually retire the stale rows (default: dry run)")
    args = parser.parse_args(argv)

    if coll is None:
        from dotenv import load_dotenv
        import pymongo
        load_dotenv()
        coll = pymongo.MongoClient(os.getenv("MONGO_URI"))["aquaflow"]["payment_transactions"]

    pairs = find_duplicate_cod_rows(coll)
    if not pairs:
        print("No duplicate cash-deposit rows found.")
        return 0

    print(f"Found {len(pairs)} duplicate pair(s):\n")
    for pair in pairs:
        stale, kept = pair["stale"], pair["kept"]
        print(f"  booking {stale['booking_id']}  driver {stale.get('driver_email')}  amount {stale.get('amount')}")
        print(f"    retire: {stale['transaction_id']} [{stale['status']}] created {stale['created_at']:%d %b %Y %H:%M}")
        print(f"    keep  : {kept['transaction_id']} [{kept['status']}] created {kept['created_at']:%d %b %Y %H:%M}")

    if not args.apply:
        print("\nDry run - nothing changed. Re-run with --apply to retire the stale rows.")
        return 0
    done = supersede_duplicate_cod_rows(coll, pairs)
    print(f"\nRetired {done} stale row(s) as 'Superseded'. Nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
