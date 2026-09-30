"""
Monthly workflow entry point.

Usage:
    python3 run_monthly_load.py --positions <path.xlsx> --transactions <path.xlsx>

Both arguments are optional (e.g. you may only have a new transaction
extract some months) - pass whichever you downloaded this time. Re-running
with an overlapping life-to-date transaction file is safe: the txn table's
unique key skips rows already loaded.
"""
import argparse
from pathlib import Path

from db import get_connection
from ingest_positions import load_positions
from ingest_transactions import load_transactions
from postprocess import detect_internal_transfers, apply_confirmed_transfers
from reconcile import run_all


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", type=Path, help="Path to position snapshot .xlsx")
    ap.add_argument("--transactions", type=Path, help="Path to transaction extract .xlsx")
    args = ap.parse_args()

    conn = get_connection()

    if args.positions:
        load_positions(args.positions, conn)
    if args.transactions:
        load_transactions(args.transactions, conn)
        detect_internal_transfers(conn)
        apply_confirmed_transfers(conn)

    run_all(conn)
    conn.close()
    print("\nDone. Run 'python3 demo_reports.py' to see summary views, "
          "or query data/portfolio.db directly.")


if __name__ == "__main__":
    main()
