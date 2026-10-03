"""Repair garbled instrument IDs left behind by OCR-read statements.

OCR turns the digit 0 into letter O or Q ('IEOOOXNHMJW8' for the real
'IE000XNHMJW8') or finds no ISIN at all ('NAME:<fund name>'). The same fund then
carries one ID in OCR-era months and another from the text-layer months on, so
instrument-level history is split. This tool finds every such ID, works out
which already-known valid ISIN it should have been (see isin_tools.py) and
re-points positions and transactions to it.

  python fix_instrument_ids.py                 # DRY RUN: report only, changes nothing
  python fix_instrument_ids.py --apply         # backs up the database, then repairs
  python fix_instrument_ids.py --institution X # default Endowus

Safety, in the same spirit as the loaders' reconciliation gates:
  * The dry run performs the real updates inside a transaction and rolls them
    back, so "the dry run passed" means "--apply will pass".
  * Before/after checks, per affected account: position and transaction row
    counts, and the sums of market value and of gross amount, must be IDENTICAL.
    No reference to a retired ID may remain. The surviving instruments' names
    must be unchanged (an OCR name never overwrites an exact one).
  * Any failed check, or a uniqueness collision, rolls everything back.
  * It never guesses: an ID with no match, or an ambiguous one, is reported and
    left alone. An ID shared with another institution is skipped.
  * Idempotent: run again and it finds nothing.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from db import get_connection, DB_PATH
from isin_tools import isin_valid, resolve_instrument_id


def _refs(conn, instrument_id):
    pos = conn.execute("SELECT count(*) FROM position_snapshot WHERE instrument_id=?", (instrument_id,)).fetchone()[0]
    tx = conn.execute("SELECT count(*) FROM txn WHERE instrument_id=?", (instrument_id,)).fetchone()[0]
    return pos, tx


def _account_totals(conn, accounts):
    out = {}
    for a in accounts:
        pc, pv = conn.execute("SELECT count(*), round(coalesce(sum(market_value_base),0),4) "
                              "FROM position_snapshot WHERE account_id=?", (a,)).fetchone()
        tc, tv = conn.execute("SELECT count(*), round(coalesce(sum(gross_amount),0),4) "
                              "FROM txn WHERE account_id=?", (a,)).fetchone()
        out[a] = (pc, pv, tc, tv)
    return out


def _dup_groups(conn, accounts):
    """Number of groups of transaction rows that are exact copies of each other. Checked
    explicitly because the table's UNIQUE constraint cannot be relied on: several of its
    columns are NULL on many rows, and SQLite treats NULLs as distinct, so re-pointing two
    rows to the same instrument can create a duplicate pair without any error."""
    ph = ",".join("?" * len(accounts))
    return conn.execute(
        f"""SELECT count(*) FROM (SELECT 1 FROM txn WHERE account_id IN ({ph})
            GROUP BY account_id, source_leg, source_file, source_reference, instrument_id,
                     trade_date, txn_type, round(gross_amount, 2) HAVING count(*) > 1)""",
        accounts).fetchone()[0]


def find_repairs(conn, institution):
    accounts = [r[0] for r in conn.execute("SELECT account_id FROM account WHERE institution_id=?", (institution,))]
    if not accounts:
        sys.exit(f"No accounts found for institution '{institution}'.")
    ph = ",".join("?" * len(accounts))
    referenced = {r[0] for r in conn.execute(
        f"""SELECT DISTINCT instrument_id FROM position_snapshot WHERE account_id IN ({ph})
            UNION SELECT DISTINCT instrument_id FROM txn WHERE account_id IN ({ph})""", accounts * 2)
        if r[0]}
    bad = sorted(i for i in referenced if not isin_valid(i))
    known = {r[0]: r[1] for r in conn.execute("SELECT instrument_id, name FROM instrument") if isin_valid(r[0])}
    names = {r[0]: r[1] for r in conn.execute("SELECT instrument_id, name FROM instrument")}

    repairs, unresolved, skipped = [], [], []
    for b in bad:
        users = [r[0] for r in conn.execute(
            """SELECT DISTINCT account_id FROM position_snapshot WHERE instrument_id=?
               UNION SELECT DISTINCT account_id FROM txn WHERE instrument_id=?""", (b, b))]
        outside = [u for u in users if u not in accounts]
        if outside:
            skipped.append((b, f"also used by account(s) outside {institution}: {outside}"))
            continue
        held = {r[0] for r in conn.execute(
            f"""SELECT DISTINCT instrument_id FROM position_snapshot WHERE account_id IN ({",".join("?" * len(users))})
                UNION SELECT DISTINCT instrument_id FROM txn WHERE account_id IN ({",".join("?" * len(users))})""",
            users * 2) if r[0] and isin_valid(r[0])}
        new_id, how = resolve_instrument_id(b, names.get(b), known, held)
        if new_id:
            repairs.append((b, new_id, how, users))
        else:
            unresolved.append((b, how))
    return accounts, repairs, unresolved, skipped, known, names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    ap.add_argument("--institution", default="Endowus")
    ap.add_argument("--db", help="database file (default: data/portfolio.db)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    conn = get_connection(db_path)
    conn.commit()
    accounts, repairs, unresolved, skipped, known, names = find_repairs(conn, args.institution)

    print(f"Institution {args.institution}: {len(repairs)} ID(s) can be repaired, "
          f"{len(unresolved)} unresolved, {len(skipped)} skipped.\n")
    for old, new, how, users in repairs:
        pos, tx = _refs(conn, old)
        print(f"  {old}\n    -> {new}   ({how})\n"
              f"       '{(names.get(old) or '')[:50]}'  =>  '{(known[new] or '')[:50]}'\n"
              f"       re-points {pos} position row(s), {tx} transaction row(s)")
    for old, why in unresolved:
        print(f"  UNRESOLVED  {old}: {why}")
    for old, why in skipped:
        print(f"  SKIPPED     {old}: {why}")
    if not repairs:
        print("Nothing to repair." if not unresolved else "\nNothing could be repaired automatically.")
        return 0

    affected = sorted({u for *_, users in repairs for u in users})
    before = _account_totals(conn, affected)
    dups_before = _dup_groups(conn, affected)
    all_before = conn.execute("SELECT (SELECT count(*) FROM position_snapshot), (SELECT count(*) FROM txn)").fetchone()
    new_ids = sorted({n for _, n, *_ in repairs})
    name_before = {n: names.get(n) for n in new_ids}

    if args.apply:
        backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        bk = sqlite3.connect(backup)
        conn.backup(bk)
        bk.close()
        print(f"\nBackup written: {backup}")

    conn.execute("BEGIN")
    try:
        for old, new, _, _ in repairs:
            conn.execute("UPDATE position_snapshot SET instrument_id=? WHERE instrument_id=?", (new, old))
            conn.execute("UPDATE txn SET instrument_id=? WHERE instrument_id=?", (new, old))
            conn.execute("""UPDATE instrument SET asset_class = COALESCE(asset_class,
                              (SELECT asset_class FROM instrument WHERE instrument_id=?)) WHERE instrument_id=?""",
                         (old, new))
            left = _refs(conn, old)
            if left != (0, 0):
                raise RuntimeError(f"{old} still referenced after re-pointing: {left}")
            conn.execute("DELETE FROM instrument WHERE instrument_id=?", (old,))

        after = _account_totals(conn, affected)
        problems = []
        for a in affected:
            if before[a] != after[a]:
                problems.append(f"{a}: (pos rows, pos value, txn rows, txn amount) {before[a]} -> {after[a]}")
        if _dup_groups(conn, affected) != dups_before:
            problems.append("re-pointing would create duplicate transaction rows "
                            f"(duplicate groups {dups_before} -> {_dup_groups(conn, affected)})")
        all_after = conn.execute("SELECT (SELECT count(*) FROM position_snapshot), (SELECT count(*) FROM txn)").fetchone()
        if tuple(all_before) != tuple(all_after):
            problems.append(f"total row counts changed {tuple(all_before)} -> {tuple(all_after)}")
        for n in new_ids:
            nm = conn.execute("SELECT name FROM instrument WHERE instrument_id=?", (n,)).fetchone()
            if nm is None or nm[0] != name_before[n]:
                problems.append(f"name of {n} changed: {name_before[n]!r} -> {nm[0] if nm else None!r}")
        if problems:
            raise RuntimeError("; ".join(problems))
    except (sqlite3.IntegrityError, RuntimeError) as e:
        conn.rollback()
        print(f"\nABORT - rolled back, nothing changed: {e}")
        return 1

    print("\nChecks passed: per-account row counts and amounts identical before/after, no duplicate "
          "transaction rows created, no retired ID still referenced, surviving names unchanged.")
    if args.apply:
        conn.commit()
        print("APPLIED. Re-run to confirm: it should report nothing left to repair.")
    else:
        conn.rollback()
        print("DRY RUN - nothing was changed. Re-run with --apply to make these changes "
              "(a backup is taken first).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
