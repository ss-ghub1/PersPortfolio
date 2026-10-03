"""Remove transaction rows that a forced reload stored a second (or third) time.

  python fix_duplicate_txns.py            # DRY RUN: report only, changes nothing
  python fix_duplicate_txns.py --apply    # backs up the database, then removes the extra copies
  python fix_duplicate_txns.py --db PATH

Background: until the IBKR, CDP, DBS and Endowus loaders were fixed, `--force` on a
statement added its transactions again (cash rows have no instrument or quantity,
SQLite treats NULLs as distinct in a UNIQUE constraint, so INSERT OR IGNORE ignored
nothing). check_duplicate_txns.py shows what is affected; this removes the copies.

What counts as a duplicate - deliberately narrow:
  * the row's reference is file-prefixed ('<statement file>:<leg>:<n>'), i.e. unique
    per row inside its file by construction. UBS rows are never touched: their
    reference is the bank's, can legitimately repeat, and some have none.
  * AND every stored field (date, type, amount, instrument, quantity, price, text ...,
    everything except the row id and load time) is identical across the copies.
  If copies of one reference DIFFER, they are reported and left alone - that is not a
  reload copy, it is something that needs a human look (for instance rows left by an
  older parser version alongside the corrected ones).
The first-loaded copy is kept; the rest are deleted.

Safety, same spirit as fix_instrument_ids.py: dry run by default; the dry run does the
real deletes inside a transaction and rolls back, so a passing dry run means --apply
will pass; the database is backed up before --apply; and before/after checks must hold
exactly - per affected account, rows removed == copies identified and the amount sum
falls by exactly the removed amounts; no other account's rows change; positions are
untouched; no duplicate remains. Any failure rolls everything back. Idempotent.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from db import get_connection, DB_PATH

SKIP_COLS = {"txn_row_id", "loaded_at"}


def _content_cols(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(txn)") if r[1] not in SKIP_COLS]


def find_groups(conn):
    cols = _content_cols(conn)
    refs = conn.execute("""
        SELECT account_id, source_file, source_leg, source_reference FROM txn
        WHERE source_reference LIKE source_file || ':%'
        GROUP BY account_id, source_file, source_leg, source_reference HAVING count(*) > 1
        ORDER BY account_id, source_file, source_leg, source_reference""").fetchall()
    exact, differing = [], []
    for a, f, leg, ref in refs:
        rows = conn.execute(f"SELECT txn_row_id, {', '.join(cols)} FROM txn "
                            "WHERE account_id=? AND source_file=? AND source_leg=? AND source_reference=? "
                            "ORDER BY txn_row_id", (a, f, leg, ref)).fetchall()
        if len({tuple(r[1:]) for r in rows}) == 1:
            exact.append((a, f, leg, ref, [r[0] for r in rows], rows[0][1 + cols.index("gross_amount")]))
        else:
            differing.append((a, f, leg, ref, len(rows)))
    return exact, differing


def _snapshot(conn, accounts):
    out = {}
    for a in accounts:
        out[a] = (conn.execute("SELECT count(*), round(coalesce(sum(gross_amount),0),4) FROM txn WHERE account_id=?", (a,)).fetchone()[:2],
                  conn.execute("SELECT count(*), round(coalesce(sum(market_value_base),0),4) FROM position_snapshot WHERE account_id=?", (a,)).fetchone()[:2])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    ap.add_argument("--db", help="database file (default: data/portfolio.db)")
    args = ap.parse_args()
    db_path = Path(args.db) if args.db else DB_PATH
    conn = get_connection(db_path)
    conn.commit()

    exact, differing = find_groups(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT account_id, coalesce(display_code, account_id) FROM account")}

    if differing:
        print(f"{len(differing)} reference(s) have copies that DIFFER from each other - left alone, needs a look:")
        for a, f, leg, ref, n in differing:
            print(f"  {names.get(a, a)}  {leg}  {ref}  ({n} rows)")
        print()
    if not exact:
        print("No reload copies found. Nothing to remove.")
        return 0

    per = {}
    for a, f, leg, ref, ids, amt in exact:
        d = per.setdefault((a, f, leg), {"refs": 0, "extra": 0, "amount": 0.0})
        d["refs"] += 1
        d["extra"] += len(ids) - 1
        d["amount"] += (len(ids) - 1) * (amt or 0.0)
    total_extra = sum(d["extra"] for d in per.values())
    print(f"{total_extra} reload cop{'y' if total_extra == 1 else 'ies'} to remove (first-loaded copy of each row is kept):\n")
    for (a, f, leg), d in per.items():
        print(f"  {names.get(a, a):<22} {leg:<9} {f[:58]}\n"
              f"      {d['refs']} row(s) stored more than once -> removing {d['extra']} extra cop{'y' if d['extra']==1 else 'ies'} "
              f"(net amount currently counted extra: {d['amount']:,.2f})")

    affected = sorted({a for a, *_ in exact})
    all_accounts = [r[0] for r in conn.execute("SELECT account_id FROM account")]
    others = [a for a in all_accounts if a not in affected]
    before = _snapshot(conn, affected)
    before_others = _snapshot(conn, others)
    to_delete = [i for *_, ids, _ in exact for i in ids[1:]]
    del_by_acct, del_amt_by_acct = {}, {}
    for a, f, leg, ref, ids, amt in exact:
        del_by_acct[a] = del_by_acct.get(a, 0) + len(ids) - 1
        del_amt_by_acct[a] = del_amt_by_acct.get(a, 0.0) + (len(ids) - 1) * (amt or 0.0)

    if args.apply:
        backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        bk = sqlite3.connect(backup)
        conn.backup(bk)
        bk.close()
        print(f"\nBackup written: {backup}")

    conn.execute("BEGIN")
    try:
        conn.executemany("DELETE FROM txn WHERE txn_row_id=?", [(i,) for i in to_delete])
        after = _snapshot(conn, affected)
        problems = []
        for a in affected:
            (bc, bs), bpos = before[a]
            (ac, as_), apos = after[a]
            if bc - ac != del_by_acct[a]:
                problems.append(f"{a}: expected {del_by_acct[a]} rows removed, {bc - ac} were")
            if abs((bs - as_) - del_amt_by_acct[a]) > 0.005:
                problems.append(f"{a}: amount fell by {bs - as_:,.4f}, expected {del_amt_by_acct[a]:,.4f}")
            if bpos != apos:
                problems.append(f"{a}: positions changed {bpos} -> {apos}")
        if _snapshot(conn, others) != before_others:
            problems.append("an account that was not supposed to change did change")
        left, _ = find_groups(conn)
        if left:
            problems.append(f"{len(left)} duplicate group(s) still present")
        if problems:
            raise RuntimeError("; ".join(problems))
    except (sqlite3.Error, RuntimeError) as e:
        conn.rollback()
        print(f"\nABORT - rolled back, nothing changed: {e}")
        return 1

    print("\nChecks passed: per account, rows removed == copies identified and the amount fell by exactly "
          "those copies; no other account changed; positions untouched; no duplicate remains.")
    if args.apply:
        conn.commit()
        print("APPLIED. Run check_duplicate_txns.py to confirm.")
    else:
        conn.rollback()
        print("DRY RUN - nothing was changed. Re-run with --apply to remove them (a backup is taken first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
