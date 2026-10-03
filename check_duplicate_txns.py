"""Read-only check: are any transaction rows in the database exact copies of each other?

  python check_duplicate_txns.py            # default database
  python check_duplicate_txns.py --db PATH

Why it exists: a forced reload (--force) of a statement used to ADD that
statement's transactions a second time in the IBKR, CDP, DBS and Endowus Joint
loaders. The table's UNIQUE constraint cannot stop it - cash-type rows have no
instrument and no quantity, and SQLite treats NULLs as distinct - so
INSERT OR IGNORE ignored nothing. Fees, income and the Transactions totals are
then overstated by the copies. (Fixed in all four loaders: a reload now replaces
the statement's rows; fix_duplicate_txns.py removes copies already stored.)

Scope, and why: the IBKR, CDP, DBS and Endowus loaders give every row a reference
of the form '<statement file>:<leg>:<row number>', unique within the file by
construction, so two rows sharing one are a reload copy - and a legitimate repeat
(e.g. two separate S$10,000 investments on one day) has a different reference and
is not flagged. UBS rows are NOT checked: their reference is the bank's own, one
reference can cover several genuinely different rows (batch transfers, partial
fills), and some have none. An earlier version of this check included them and
reported 123 "duplicates" in Portfolio 4 that were in fact separate transactions.
Changes nothing; exits 1 if it finds any.
"""
import argparse
import sys
from pathlib import Path

from db import get_connection, DB_PATH


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="database file (default: data/portfolio.db)")
    args = ap.parse_args()
    conn = get_connection(Path(args.db) if args.db else DB_PATH)

    rows = conn.execute("""
        SELECT a.display_code, t.account_id, t.source_file, t.source_leg,
               count(*) AS refs, sum(n - 1) AS extra_rows, round(sum((n - 1) * amt), 2) AS extra_amount
        FROM (SELECT account_id, source_file, source_leg, source_reference, count(*) AS n,
                     max(gross_amount) AS amt
              FROM txn WHERE source_reference LIKE source_file || ':%'
              GROUP BY account_id, source_file, source_leg, source_reference
              HAVING count(*) > 1) t
        JOIN account a ON a.account_id = t.account_id
        GROUP BY t.account_id, t.source_file, t.source_leg
        ORDER BY a.institution_id, t.account_id, t.source_file""").fetchall()

    if not rows:
        print("No duplicated transaction rows found.")
        return 0
    total = sum(r["extra_rows"] for r in rows)
    print(f"{total} duplicated transaction row(s) found:\n")
    for r in rows:
        print(f"  {r['display_code']:<26} {r['source_leg']:<9} {r['source_file'][:60]}\n"
              f"      {r['refs']} row(s) stored more than once -> {r['extra_rows']} extra copy/copies, "
              f"net amount counted twice: {r['extra_amount']:,.2f}")
    print("\nThese inflate the Fees, Income and Transactions pages. Remove the extra copies (the first-loaded "
          "copy of each row is kept) with:\n    python fix_duplicate_txns.py            # dry run first\n"
          "    python fix_duplicate_txns.py --apply    # backs up the database, then removes them")
    return 1


if __name__ == "__main__":
    sys.exit(main())
