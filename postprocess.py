"""
Internal-transfer detection.

Classification rules alone can't tell a genuine external cash flow apart
from money simply moving between two of the user's own accounts at the
same institution - both can carry a generic label like 'Special payment
order' or 'credit'. But an internal transfer always leaves a fingerprint:
same trade date, same currency, matching absolute amount, opposite sign,
in two DIFFERENT accounts. This pass finds those pairs among the rows a
classification_rule left as NEEDS_REVIEW and re-tags them as TRANSFER,
so they're correctly excluded from external-cash-flow-based performance
calculations (e.g. Modified Dietz) while genuine deposits/withdrawals stay
tagged for that purpose.

Run this AFTER loading all available extracts (it works across accounts),
and re-run it whenever new transaction rows are loaded.
"""
import sqlite3
from db import get_connection


def detect_internal_transfers(conn: sqlite3.Connection):
    # Broad candidate pool: any cash-leg row whose type could plausibly be one
    # side of a same-institution internal transfer. Classification rules run
    # BEFORE this step and may already have tagged a row DEPOSIT/WITHDRAWAL
    # (e.g. a generic 'Special payment order' rule) - this pass overrides
    # that tag when a matching opposite-sign pair turns up in another of the
    # user's own accounts, since that pairing is stronger evidence than a
    # generic label match.
    candidates = conn.execute("""
        SELECT txn_row_id, account_id, trade_date, currency, gross_amount
        FROM txn
        WHERE source_leg = 'CASH'
          AND (txn_subtype = 'NEEDS_REVIEW'
               OR txn_type IN ('DEPOSIT', 'WITHDRAWAL', 'CASH_MOVEMENT')
               OR txn_subtype = 'INTERNAL_TRANSFER_UNMATCHED')
    """).fetchall()

    # group by (trade_date, currency, abs(amount))
    groups = {}
    for row in candidates:
        key = (row["trade_date"], row["currency"], round(abs(row["gross_amount"]), 2))
        groups.setdefault(key, []).append(row)

    n_matched = 0
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        positives = [r for r in rows if r["gross_amount"] > 0]
        negatives = [r for r in rows if r["gross_amount"] < 0]
        # pair one inflow with one outflow in a DIFFERENT account
        for pos in positives:
            match = next((neg for neg in negatives
                          if neg["account_id"] != pos["account_id"]), None)
            if not match:
                continue
            conn.execute(
                "UPDATE txn SET txn_type='TRANSFER', txn_subtype='INTERNAL_TRANSFER_IN' "
                "WHERE txn_row_id=?", (pos["txn_row_id"],))
            conn.execute(
                "UPDATE txn SET txn_type='TRANSFER', txn_subtype='INTERNAL_TRANSFER_OUT' "
                "WHERE txn_row_id=?", (match["txn_row_id"],))
            negatives.remove(match)
            n_matched += 2

    conn.commit()
    print(f"[postprocess] re-tagged {n_matched} rows as internal transfers "
          f"({n_matched // 2} matched pairs)")
    return n_matched


def apply_confirmed_transfers(conn: sqlite3.Connection, config_path="config/confirmed_transfers.csv"):
    """Applies user-confirmed transfer destinations for cases the automatic
    pairing can't resolve on its own (typically because the destination
    account has no cash ledger of its own, so there's no matching leg to
    pair against - e.g. UBS-0004/P04, which only has security-leg data)."""
    import csv
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return 0

    n_matched = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            cur = conn.execute(
                """UPDATE txn SET txn_type='TRANSFER',
                     txn_subtype=?
                   WHERE account_id=? AND trade_date=? AND currency=?
                     AND ABS(gross_amount - ?) < 0.01 AND source_leg='CASH'""",
                (f"CONFIRMED_TRANSFER_TO_{row['destination_account_id']}",
                 row["account_id"], row["trade_date"], row["currency"],
                 float(row["gross_amount"])),
            )
            n_matched += cur.rowcount
    conn.commit()
    print(f"[postprocess] applied {n_matched} confirmed transfer overrides from config/confirmed_transfers.csv")
    return n_matched


if __name__ == "__main__":
    conn = get_connection()
    detect_internal_transfers(conn)
    apply_confirmed_transfers(conn)
    conn.close()
