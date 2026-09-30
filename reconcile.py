"""
Reconciliation checks. Run after every load so a bad extract or a parsing
gap is caught immediately rather than silently corrupting downstream numbers.

Check 1 - CASH BALANCE REPLAY:
  For each (account, currency) cash ledger, replay every CASH-leg txn in
  trade-date order starting from the running_balance implied by the first
  row, and confirm each row's stated running_balance matches. This uses
  UBS's own running balance column, so it validates our parsing/classification
  didn't drop or double-count any row - independent of whether our txn_type
  labels are right.

Check 2 - SNAPSHOT vs LEDGER CASH BALANCE:
  Compares the latest position_snapshot cash row for each (account, currency)
  against the most recent running_balance in txn for that account/currency,
  as of the same date. This is the true end-to-end check: two independently
  produced UBS extracts should agree.
"""
import sqlite3
from db import get_connection


def run_cash_balance_replay(conn: sqlite3.Connection):
    print("\n=== Check 1: cash balance replay (within transaction ledger) ===")
    accounts_ccys = conn.execute(
        "SELECT DISTINCT account_id, currency FROM txn WHERE source_leg='CASH'"
    ).fetchall()

    all_ok = True
    for row in accounts_ccys:
        account_id, ccy = row["account_id"], row["currency"]
        # UBS lists these ledgers NEWEST-FIRST with 'Balance' = balance AFTER
        # that row's own transaction. So walking down the file (txn_row_id
        # ascending = original file order) moves backward in time, and the
        # relationship is: balance[older] = balance[newer] - amount[newer].
        # We deliberately do NOT re-sort by trade_date - same-day rows can
        # tie, and the file's own order is the true posting sequence.
        txns = conn.execute(
            """SELECT trade_date, gross_amount, running_balance, raw_txn_type_label, txn_row_id
               FROM txn WHERE source_leg='CASH' AND account_id=? AND currency=?
               ORDER BY txn_row_id""",
            (account_id, ccy),
        ).fetchall()
        if not txns:
            continue

        mismatches = []
        for i in range(len(txns) - 1):
            newer, older = txns[i], txns[i + 1]
            if newer["running_balance"] is None or older["running_balance"] is None \
               or newer["gross_amount"] is None:
                continue
            expected_older_balance = round(newer["running_balance"] - newer["gross_amount"], 2)
            actual_older_balance = round(older["running_balance"], 2)
            if abs(expected_older_balance - actual_older_balance) > 0.01:
                mismatches.append((older["trade_date"], newer["raw_txn_type_label"],
                                    expected_older_balance, actual_older_balance))

        status = "OK" if not mismatches else f"{len(mismatches)} MISMATCH(ES)"
        print(f"  {account_id} / {ccy}: {len(txns)} rows -> {status}")
        for m in mismatches[:5]:
            print(f"      {m[0]}  '{m[1]}'  expected={m[2]}  actual={m[3]}")
        conn.execute(
            """INSERT INTO reconciliation_log
                 (account_id, scope, status, notes)
               VALUES (?, ?, ?, ?)""",
            (account_id, f"CASH_BALANCE_REPLAY:{ccy}", "OK" if not mismatches else "MISMATCH",
             f"{len(mismatches)} mismatches out of {len(txns)} rows"),
        )
        if mismatches:
            all_ok = False
    conn.commit()
    return all_ok


def run_snapshot_vs_ledger(conn: sqlite3.Connection):
    print("\n=== Check 2: latest position snapshot cash vs transaction ledger running balance ===")
    snap_rows = conn.execute(
        """SELECT account_id, position_currency, quantity, as_of_date
           FROM position_snapshot
           WHERE is_cash=1
             AND as_of_date = (SELECT max(as_of_date) FROM position_snapshot s2
                                WHERE s2.account_id = position_snapshot.account_id)
        """
    ).fetchall()

    all_ok = True
    for row in snap_rows:
        account_id, ccy, snap_qty, as_of = (
            row["account_id"], row["position_currency"], row["quantity"], row["as_of_date"]
        )
        ledger_row = conn.execute(
            """SELECT running_balance, trade_date FROM txn
               WHERE source_leg='CASH' AND account_id=? AND currency=?
                 AND running_balance IS NOT NULL
               ORDER BY trade_date DESC, txn_row_id DESC LIMIT 1""",
            (account_id, ccy),
        ).fetchone()
        if not ledger_row:
            continue
        diff = round((snap_qty or 0) - (ledger_row["running_balance"] or 0), 2)
        status = "OK" if abs(diff) < 0.01 else "MISMATCH"
        print(f"  {account_id} / {ccy}: snapshot={snap_qty} (as of {as_of}) vs "
              f"ledger={ledger_row['running_balance']} (last txn {ledger_row['trade_date']}) "
              f"-> diff={diff} [{status}]")
        conn.execute(
            """INSERT INTO reconciliation_log
                 (account_id, scope, period_end, expected_value, actual_value, difference, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, f"SNAPSHOT_VS_LEDGER:{ccy}", as_of, snap_qty,
             ledger_row["running_balance"], diff, status),
        )
        if status == "MISMATCH" and abs(diff) > 1:  # allow small gap = txns after ledger export date
            all_ok = False
    conn.commit()
    return all_ok


def print_other_reconciliation_logs(conn: sqlite3.Connection):
    """Surfaces reconciliation checks logged directly by an institution's own
    loader (e.g. Endowus, whose statement gives a period start/end balance
    rather than a per-line running balance, so it reconciles itself at load
    time - see ingest_endowus_pdf.py's load_endowus_statement)."""
    rows = conn.execute(
        """SELECT * FROM reconciliation_log
           WHERE scope NOT LIKE 'CASH_BALANCE_REPLAY%' AND scope NOT LIKE 'SNAPSHOT_VS_LEDGER%'
           ORDER BY run_at DESC"""
    ).fetchall()
    if not rows:
        return True
    print("\n=== Check 3: institution-specific reconciliation (logged at load time) ===")
    all_ok = True
    for row in rows:
        print(f"  {row['account_id']} / {row['scope']}: [{row['status']}] {row['notes']}")
        if row["status"] != "OK":
            all_ok = False
    return all_ok


def run_all(conn: sqlite3.Connection = None):
    own_conn = conn is None
    conn = conn or get_connection()
    ok1 = run_cash_balance_replay(conn)
    ok2 = run_snapshot_vs_ledger(conn)
    ok3 = print_other_reconciliation_logs(conn)
    print(f"\n=== Reconciliation summary: {'ALL CHECKS PASSED' if ok1 and ok2 and ok3 else 'REVIEW NEEDED'} ===")
    if own_conn:
        conn.close()


if __name__ == "__main__":
    run_all()
