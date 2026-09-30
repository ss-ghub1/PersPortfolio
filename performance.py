"""
Modified Dietz return calculation.

We only have ONE position snapshot so far, so we can't yet compute
period-over-period (e.g. monthly) returns - that needs a beginning AND
ending market value, which accumulates as you load more monthly snapshots.
What we CAN compute today is a defensible LIFE-TO-DATE return: treat the
account's first ever transaction date as day zero with BMV=0, and the
latest snapshot as the ending value, with every external cash flow in
between weighted by how long it's been invested.

    R = (EMV - BMV - net_CF) / (BMV + sum(CF_i * w_i))
    w_i = (total_days - days_from_period_start_to_flow_date) / total_days

Two views, because "external" means different things at each level:

  - PER-ACCOUNT: a transfer INTO/OUT OF this specific account counts as a
    flow for that account's own return (e.g. money moving from account 1
    to fund account R800's purchases is a real outflow for account 1).
  - CONSOLIDATED (all your UBS accounts together): those same internal
    transfers net to zero - only genuine deposits/withdrawals from outside
    UBS altogether count.

Caveat: cash flows in a currency other than the account's (or SGD, for
the consolidated view) are converted using the MOST RECENT available FX
rate, not the rate on the actual flow date - directionally right, not
precise. A historical daily FX table is a future improvement.
"""
from datetime import datetime
from db import get_connection


def get_latest_rate(conn, from_ccy, to_ccy="SGD"):
    if from_ccy == to_ccy:
        return 1.0
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE from_ccy=? AND to_ccy=? ORDER BY rate_date DESC LIMIT 1",
        (from_ccy, to_ccy),
    ).fetchone()
    if row:
        return row["rate"]
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE from_ccy=? AND to_ccy=? ORDER BY rate_date DESC LIMIT 1",
        (to_ccy, from_ccy),
    ).fetchone()
    return (1.0 / row["rate"]) if row else None


def _days(d1, d2):
    return (datetime.fromisoformat(d2) - datetime.fromisoformat(d1)).days


def modified_dietz(bmv, emv, flows, period_start, period_end):
    """flows: list of (date_iso, amount) already converted to the target currency.
    Returns (return_pct, annualized_pct) or (None, None) if not computable."""
    total_days = _days(period_start, period_end)
    if total_days <= 0:
        return None, None

    net_cf = sum(amt for _, amt in flows)
    weighted_cf = 0.0
    for dt, amt in flows:
        days_in = _days(period_start, dt)
        w = (total_days - days_in) / total_days
        weighted_cf += amt * w

    denom = bmv + weighted_cf
    if denom == 0:
        return None, None

    r = (emv - bmv - net_cf) / denom
    annualized = (1 + r) ** (365.0 / total_days) - 1 if total_days > 0 else None
    return r, annualized


def statement_performance(conn, account_id):
    """Reports UBS's OWN computed TWR history for an account, loaded from
    its statement PDF(s). This is authoritative - no assumptions needed."""
    acct = conn.execute("SELECT * FROM account WHERE account_id=?", (account_id,)).fetchone()
    annual = conn.execute(
        """SELECT * FROM account_valuation_history
           WHERE account_id=? AND period_type='year_end' ORDER BY period_end""",
        (account_id,),
    ).fetchall()
    monthly = conn.execute(
        """SELECT * FROM account_valuation_history
           WHERE account_id=? AND period_type='month_end' ORDER BY period_end DESC""",
        (account_id,),
    ).fetchall()
    since_inception = conn.execute(
        """SELECT * FROM account_valuation_history
           WHERE account_id=? AND period_type='cumulative_annual'
           ORDER BY period_end DESC LIMIT 1""",
        (account_id,),
    ).fetchone()
    if not annual and not monthly:
        return None
    latest = monthly[0] if monthly else annual[-1]
    return {
        "account_id": account_id, "account_label": acct["account_label"],
        "currency": latest["currency"],
        "as_of": since_inception["period_end"] if since_inception else latest["period_end"],
        "since_inception_twr_pct": since_inception["twr_pct"] if since_inception else None,
        "annual": annual,
        "monthly": monthly,
    }


def print_statement_performance(conn, account_id):
    p = statement_performance(conn, account_id)
    if not p:
        print(f"\n{account_id}: no statement PDF loaded yet.")
        return
    print(f"\n{p['account_label']} ({p['currency']}) - from UBS's own statement TWR")
    if p["since_inception_twr_pct"] is not None:
        print(f"  Since inception, cumulative TWR as of {p['as_of']}: {p['since_inception_twr_pct']:.2f}%")
    else:
        print(f"  Since-inception figure not available (statement PDF didn't include the full annual table)")
    print(f"  Year by year:")
    for row in p["annual"]:
        flows = ""
        if row["inflows"] or row["outflows"]:
            flows = f"  (in: {row['inflows'] or 0:,.0f}, out: {row['outflows'] or 0:,.0f})"
        print(f"    {row['period_label']:<6} TWR {row['twr_pct']:>7.2f}%   "
              f"final value {row['final_value']:>12,.0f} {p['currency']}{flows}")


def account_performance(conn, account_id):
    acct = conn.execute("SELECT * FROM account WHERE account_id=?", (account_id,)).fetchone()
    base_ccy = acct["base_currency"]

    period_start = conn.execute(
        "SELECT min(trade_date) FROM txn WHERE account_id=?", (account_id,)
    ).fetchone()[0]
    snap = conn.execute(
        """SELECT as_of_date, sum(market_value_base) emv FROM position_snapshot
           WHERE account_id=? AND as_of_date=(SELECT max(as_of_date) FROM position_snapshot WHERE account_id=?)""",
        (account_id, account_id),
    ).fetchone()
    if not period_start or not snap or snap["emv"] is None:
        return None
    period_end = snap["as_of_date"]
    emv = snap["emv"]

    flow_rows = conn.execute(
        """SELECT trade_date, gross_amount, currency FROM txn
           WHERE account_id=? AND source_leg='CASH'
             AND txn_type IN ('DEPOSIT','WITHDRAWAL','TRANSFER')""",
        (account_id,),
    ).fetchall()
    flows = []
    for row in flow_rows:
        rate = get_latest_rate(conn, row["currency"], base_ccy)
        if rate is None:
            continue
        flows.append((row["trade_date"], row["gross_amount"] * rate))

    r, ann = modified_dietz(0.0, emv, flows, period_start, period_end)
    return {
        "account_id": account_id, "account_label": acct["account_label"],
        "base_currency": base_ccy, "period_start": period_start, "period_end": period_end,
        "emv": emv, "net_external_cf": sum(a for _, a in flows), "n_flows": len(flows),
        "return_pct": r, "annualized_pct": ann,
    }


def consolidated_performance(conn, account_ids):
    period_start = conn.execute(
        f"SELECT min(trade_date) FROM txn WHERE account_id IN ({','.join('?' * len(account_ids))})",
        account_ids,
    ).fetchone()[0]

    emv_sgd = 0.0
    period_end = None
    for account_id in account_ids:
        acct = conn.execute("SELECT base_currency FROM account WHERE account_id=?", (account_id,)).fetchone()
        snap = conn.execute(
            """SELECT as_of_date, sum(market_value_base) emv FROM position_snapshot
               WHERE account_id=? AND as_of_date=(SELECT max(as_of_date) FROM position_snapshot WHERE account_id=?)""",
            (account_id, account_id),
        ).fetchone()
        if not snap or snap["emv"] is None:
            continue
        period_end = max(period_end or snap["as_of_date"], snap["as_of_date"])
        rate = get_latest_rate(conn, acct["base_currency"], "SGD")
        emv_sgd += snap["emv"] * (rate or 0)

    # Consolidated: ONLY genuine external deposits/withdrawals count - internal
    # transfers between the user's own tracked accounts must be excluded here,
    # even the ones we couldn't pair 1:1 (they still stay inside the household).
    placeholders = ",".join("?" * len(account_ids))
    flow_rows = conn.execute(
        f"""SELECT trade_date, gross_amount, currency FROM txn
            WHERE account_id IN ({placeholders}) AND source_leg='CASH'
              AND txn_type IN ('DEPOSIT','WITHDRAWAL')""",
        account_ids,
    ).fetchall()
    flows = []
    for row in flow_rows:
        rate = get_latest_rate(conn, row["currency"], "SGD")
        if rate is None:
            continue
        flows.append((row["trade_date"], row["gross_amount"] * rate))

    r, ann = modified_dietz(0.0, emv_sgd, flows, period_start, period_end)
    return {
        "period_start": period_start, "period_end": period_end, "emv_sgd": emv_sgd,
        "net_external_cf_sgd": sum(a for _, a in flows), "n_flows": len(flows),
        "return_pct": r, "annualized_pct": ann,
    }


def main():
    conn = get_connection()
    accounts = conn.execute("SELECT account_id FROM account ORDER BY institution_id, account_id").fetchall()
    account_ids = [a["account_id"] for a in accounts]

    print("=" * 78)
    print("PERFORMANCE - all accounts")
    print("Source: each institution's own statement-based TWR where available")
    print("(authoritative - no assumptions needed).")
    print("=" * 78)

    for account_id in account_ids:
        print_statement_performance(conn, account_id)

    print(f"\n{'=' * 78}")
    print("The transaction-derived Modified Dietz calc below (BMV=0 assumption) is")
    print("superseded by the statement-based figures above wherever both exist - it's")
    print("shown only for the pre-statement-history accounts/periods and for comparison.")
    print("=" * 78)

    for account_id in ("UBS-0001", "UBS-0002"):
        p = account_performance(conn, account_id)
        if not p:
            continue
        print(f"\n[Transaction-derived, BMV=0 assumption - NOT reliable, shown for comparison only]")
        print(f"{p['account_label']}: {p['return_pct']*100 if p['return_pct'] else float('nan'):.2f}% "
              f"(vs statement TWR above)")

    print(f"\n{'UBS-0004':<12} - statement TWR only covers from 26 June 2026 onward (mandate")
    print("               change); no UBS-computed TWR exists yet for the pre-June-2026")
    print("               history our own transaction data shows (back to Nov 2024).")

    conn.close()


if __name__ == "__main__":
    main()
