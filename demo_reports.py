"""
Example queries against the loaded data, showing the model answering the
original questions: portfolio view by account, fees, income - each with an
SGD-equivalent column using the FX rates captured from the position file.

Note on FX: rates are captured only as of each position snapshot's date, so
there's one rate per currency pair per snapshot load. Converting older
transactions (fees/income) uses the MOST RECENT available rate as an
approximation, not the rate on the actual transaction date - fine for a
rough total, not for precise historical SGD P&L. A proper historical FX
table (or daily rates from a market data source) is a later improvement.
"""
from db import get_connection


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def get_latest_rate(conn, from_ccy, to_ccy="SGD"):
    if from_ccy == to_ccy:
        return 1.0
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE from_ccy=? AND to_ccy=? ORDER BY rate_date DESC LIMIT 1",
        (from_ccy, to_ccy),
    ).fetchone()
    if row:
        return row["rate"]
    # try the inverse pair
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE from_ccy=? AND to_ccy=? ORDER BY rate_date DESC LIMIT 1",
        (to_ccy, from_ccy),
    ).fetchone()
    return (1.0 / row["rate"]) if row else None


def main():
    conn = get_connection()

    section("1. Portfolio value by account (latest snapshot) - with SGD equivalent")
    grand_total_sgd = 0.0
    for row in conn.execute("""
        SELECT a.account_label, a.base_currency,
               round(sum(p.market_value_base), 0) AS market_value,
               max(p.as_of_date) AS as_of
        FROM position_snapshot p JOIN account a ON a.account_id = p.account_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2
                               WHERE p2.account_id = p.account_id)
        GROUP BY a.account_id ORDER BY market_value DESC
    """):
        rate = get_latest_rate(conn, row["base_currency"])
        sgd_value = row["market_value"] * rate if rate else None
        grand_total_sgd += sgd_value or 0
        sgd_str = f"{sgd_value:>14,.0f} SGD" if sgd_value is not None else "  (no FX rate)"
        print(f"  {row['account_label']:<32} {row['market_value']:>14,.0f} {row['base_currency']:<4}"
              f"  ->  {sgd_str}")
    print(f"  {'-' * 76}")
    print(f"  {'TOTAL':<32} {'':>19} {'->':>4}  {grand_total_sgd:>14,.0f} SGD")

    section("2. Asset allocation by asset class (all accounts, latest snapshot) - SGD equivalent")
    class_totals = {}
    for row in conn.execute("""
        SELECT COALESCE(i.asset_class, 'Cash') AS asset_class, a.base_currency,
               sum(p.market_value_base) AS market_value
        FROM position_snapshot p
        JOIN account a ON a.account_id = p.account_id
        LEFT JOIN instrument i ON i.instrument_id = p.instrument_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2
                               WHERE p2.account_id = p.account_id)
        GROUP BY 1, a.base_currency
    """):
        rate = get_latest_rate(conn, row["base_currency"])
        sgd_value = (row["market_value"] or 0) * (rate or 0)
        class_totals[row["asset_class"]] = class_totals.get(row["asset_class"], 0) + sgd_value
    total_sgd = sum(class_totals.values())
    for asset_class, sgd_value in sorted(class_totals.items(), key=lambda x: -x[1]):
        pct = (sgd_value / total_sgd * 100) if total_sgd else 0
        print(f"  {asset_class:<40} {sgd_value:>14,.0f} SGD   ({pct:5.1f}%)")
    print(f"  {'-' * 76}")
    print(f"  {'TOTAL':<40} {total_sgd:>14,.0f} SGD")

    section("3. Fees paid, by subtype (life to date, all accounts) - with SGD equivalent (approx, current FX)")
    fee_total_sgd = 0.0
    for row in conn.execute("""
        SELECT a.account_label, t.txn_subtype,
               round(sum(t.gross_amount), 2) AS total, t.currency, count(*) n
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = 'FEE'
          AND (t.source_leg = 'CASH'
               OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                     SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                       AND t2.txn_type = 'FEE' AND t2.source_leg = 'CASH')))
        GROUP BY a.account_id, t.txn_subtype, t.currency
        ORDER BY a.account_label, total
    """):
        rate = get_latest_rate(conn, row["currency"])
        sgd_value = row["total"] * rate if rate else None
        fee_total_sgd += sgd_value or 0
        sgd_str = f"{sgd_value:>10,.2f} SGD" if sgd_value is not None else ""
        print(f"  {row['account_label']:<24} {row['txn_subtype']:<24} "
              f"{row['total']:>10,.2f} {row['currency']:<4} ({row['n']} txns)  ->  {sgd_str}")
    print(f"  {'-' * 76}")
    print(f"  {'TOTAL FEES':<50}  ->  {fee_total_sgd:>10,.2f} SGD")

    section("4. Income received (dividends), by year - with SGD equivalent (approx, current FX)")
    income_total_sgd = 0.0
    for row in conn.execute("""
        SELECT a.account_label, substr(t.trade_date,1,4) AS yr, t.currency,
               round(sum(t.gross_amount), 2) AS total, count(*) n
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = 'INCOME'
          AND (t.source_leg = 'CASH'
               OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                     SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                       AND t2.txn_type = 'INCOME' AND t2.source_leg = 'CASH')))
        GROUP BY a.account_id, yr, t.currency
        ORDER BY a.account_label, yr
    """):
        rate = get_latest_rate(conn, row["currency"])
        sgd_value = row["total"] * rate if rate else None
        income_total_sgd += sgd_value or 0
        sgd_str = f"{sgd_value:>10,.2f} SGD" if sgd_value is not None else ""
        print(f"  {row['account_label']:<24} {row['yr']}  {row['total']:>10,.2f} "
              f"{row['currency']:<4} ({row['n']} payments)  ->  {sgd_str}")
    print(f"  {'-' * 76}")
    print(f"  {'TOTAL INCOME':<50}  ->  {income_total_sgd:>10,.2f} SGD")

    section("5. Rows still needing manual classification (review queue)")
    rows = conn.execute("""
        SELECT account_id, source_leg, raw_txn_type_label, raw_description,
               gross_amount, currency, trade_date
        FROM txn WHERE txn_subtype IN ('NEEDS_REVIEW', 'UNCLASSIFIED')
        ORDER BY trade_date DESC
    """).fetchall()
    if not rows:
        print("  (none)")
    for row in rows:
        print(f"  {row['trade_date']}  {row['account_id']:<10} {row['source_leg']:<9} "
              f"'{row['raw_txn_type_label']}' / '{row['raw_description']}'  "
              f"{row['gross_amount']:>12,.2f} {row['currency']}")

    conn.close()


if __name__ == "__main__":
    main()
