"""
Builds a snapshot Excel workbook from the current database: an Overview,
FX Rates reference, Positions, Fees, Income, and Performance tab. Re-run
after every monthly load to get a fresh snapshot you can open and browse,
without needing a database client.

Usage:
    python3 export_excel.py [output_path]
"""
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import PieChart, Reference

from db import get_connection

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14)
SUBTITLE_FONT = Font(name=FONT_NAME, italic=True, size=9, color="666666")
BOLD = Font(name=FONT_NAME, bold=True, size=10)
NORMAL = Font(name=FONT_NAME, size=10)
LINK_FONT = Font(name=FONT_NAME, size=10, color="008000")  # green = cross-sheet link
THIN = Side(style="thin", color="CCCCCC")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
CCY_FMT = '#,##0;(#,##0)'
PCT_FMT = '0.0%'

OUTPUT_DEFAULT = str(Path(__file__).resolve().parent / "output" / "portfolio_snapshot.xlsx")


def style_header_row(ws, row, ncols, start_col=1):
    for c in range(start_col, start_col + ncols):
        cell = ws.cell(row=row, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER


def autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def title_block(ws, title, subtitle):
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = subtitle
    ws["A2"].font = SUBTITLE_FONT


# ---------------------------------------------------------------------------
def build_fx_sheet(wb, conn):
    ws = wb.create_sheet("FX Rates")
    title_block(ws, "FX Rates to SGD", "Source: latest rate captured from the UBS position snapshot's own embedded FX table. Update manually if a fresher rate is needed.")

    headers = ["Currency", "Rate to SGD", "As of"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))

    rows = conn.execute("""
        SELECT from_ccy AS ccy, rate, rate_date FROM fx_rate
        WHERE to_ccy='SGD'
        AND rate_date = (SELECT max(rate_date) FROM fx_rate f2 WHERE f2.from_ccy=fx_rate.from_ccy AND f2.to_ccy='SGD')
        ORDER BY ccy
    """).fetchall()
    r += 1
    ws.cell(row=r, column=1, value="SGD").font = NORMAL
    ws.cell(row=r, column=2, value=1.0).font = Font(name=FONT_NAME, size=10, color="0000FF")
    ws.cell(row=r, column=2).number_format = '0.0000'
    ws.cell(row=r, column=3, value="(base currency)").font = NORMAL
    for row in rows:
        r += 1
        ws.cell(row=r, column=1, value=row["ccy"]).font = NORMAL
        c = ws.cell(row=r, column=2, value=row["rate"])
        c.font = Font(name=FONT_NAME, size=10, color="0000FF")  # blue = hardcoded input
        c.number_format = '0.0000'
        ws.cell(row=r, column=3, value=row["rate_date"]).font = NORMAL
    for row in range(5, r + 1):
        for col in (1, 2, 3):
            ws.cell(row=row, column=col).border = BORDER
    autosize(ws, [12, 14, 14])
    return r  # last data row, for INDEX/MATCH range reference


def fx_formula(currency_cell, native_value_cell, fx_last_row):
    """=native_value * VLOOKUP(currency, 'FX Rates' table, rate col, 0), with SGD short-circuited to 1."""
    rng = f"'FX Rates'!$A$5:$B${fx_last_row}"
    return (f'=IF({currency_cell}="SGD",{native_value_cell},'
            f'{native_value_cell}*VLOOKUP({currency_cell},{rng},2,0))')


# ---------------------------------------------------------------------------
def build_overview_sheet(wb, conn, fx_last_row, as_of_dates):
    ws = wb.create_sheet("Overview", 0)
    title_block(ws, "Portfolio Overview", f"Position data as of {as_of_dates}. Generated {datetime.now():%Y-%m-%d %H:%M}.")

    headers = ["Account", "Native Currency", "Market Value (Native)", "Market Value (SGD)"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))
    first_data_row = r + 1

    rows = conn.execute("""
        SELECT a.account_label, a.base_currency,
               sum(p.market_value_base) AS mv
        FROM position_snapshot p JOIN account a ON a.account_id = p.account_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2 WHERE p2.account_id = p.account_id)
        GROUP BY a.account_id ORDER BY mv DESC
    """).fetchall()

    r = first_data_row
    for row in rows:
        ws.cell(row=r, column=1, value=row["account_label"]).font = NORMAL
        ws.cell(row=r, column=2, value=row["base_currency"]).font = NORMAL
        cv = ws.cell(row=r, column=3, value=row["mv"])
        cv.font = NORMAL
        cv.number_format = CCY_FMT
        csgd = ws.cell(row=r, column=4)
        csgd.value = fx_formula(f"$B{r}", f"$C{r}", fx_last_row)
        csgd.font = LINK_FONT
        csgd.number_format = CCY_FMT
        for col in range(1, 5):
            ws.cell(row=r, column=col).border = BORDER
        r += 1
    last_data_row = r - 1

    ws.cell(row=r, column=1, value="TOTAL").font = BOLD
    total_cell = f"D{r}"
    tc = ws.cell(row=r, column=4, value=f"=SUM(D{first_data_row}:D{last_data_row})")
    tc.font = BOLD
    tc.number_format = CCY_FMT
    for col in range(1, 5):
        ws.cell(row=r, column=col).border = BORDER
    total_row = r

    # Asset allocation block
    r += 3
    ws.cell(row=r, column=1, value="Asset Allocation (all accounts, latest snapshot)").font = BOLD
    r += 1
    alloc_header_row = r
    for i, h in enumerate(["Asset Class", "Value (SGD)", "% of Total"], start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, 3)
    r += 1
    alloc_first_row = r

    alloc_rows = conn.execute("""
        SELECT COALESCE(i.asset_class, 'Cash') AS asset_class, a.base_currency,
               sum(p.market_value_base) AS mv
        FROM position_snapshot p
        JOIN account a ON a.account_id = p.account_id
        LEFT JOIN instrument i ON i.instrument_id = p.instrument_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2 WHERE p2.account_id = p.account_id)
        GROUP BY 1, a.base_currency
    """).fetchall()
    # pre-aggregate in Python by asset class (SGD conversion needs a rate per currency;
    # values are already per-currency subtotals, so convert then sum in Python here -
    # this one block is computed, not formula-linked, since it aggregates across a
    # currency dimension the sheet doesn't otherwise expose row by row)
    class_totals = {}
    rate_lookup = {row["ccy"]: row["rate"] for row in conn.execute(
        "SELECT from_ccy AS ccy, rate FROM fx_rate WHERE to_ccy='SGD' "
        "AND rate_date=(SELECT max(rate_date) FROM fx_rate f2 WHERE f2.from_ccy=fx_rate.from_ccy AND f2.to_ccy='SGD')"
    )}
    rate_lookup["SGD"] = 1.0
    for row in alloc_rows:
        rate = rate_lookup.get(row["base_currency"], 0)
        class_totals[row["asset_class"]] = class_totals.get(row["asset_class"], 0) + (row["mv"] or 0) * rate

    for asset_class, val in sorted(class_totals.items(), key=lambda x: -x[1]):
        ws.cell(row=r, column=1, value=asset_class).font = NORMAL
        cv = ws.cell(row=r, column=2, value=round(val, 0))
        cv.font = NORMAL
        cv.number_format = CCY_FMT
        cp = ws.cell(row=r, column=3, value=f"=B{r}/${total_cell}")
        cp.font = NORMAL
        cp.number_format = PCT_FMT
        for col in range(1, 4):
            ws.cell(row=r, column=col).border = BORDER
        r += 1
    alloc_last_row = r - 1

    # Pie chart
    chart = PieChart()
    chart.title = "Asset Allocation (SGD)"
    data = Reference(ws, min_col=2, min_row=alloc_header_row, max_row=alloc_last_row)
    cats = Reference(ws, min_col=1, min_row=alloc_first_row, max_row=alloc_last_row)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.height, chart.width = 9, 14
    ws.add_chart(chart, f"F4")

    autosize(ws, [42, 16, 22, 20])
    return total_row


# ---------------------------------------------------------------------------
def build_positions_sheet(wb, conn, fx_last_row):
    ws = wb.create_sheet("Positions")
    title_block(ws, "Positions (latest snapshot per account)", "One row per holding. SGD column is formula-linked to the FX Rates tab.")

    headers = ["Account", "Asset Class", "Instrument", "ISIN", "Currency",
               "Quantity", "Market Value (Native)", "Market Value (SGD)", "As Of"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))
    first_row = r + 1

    rows = conn.execute("""
        SELECT a.account_label, COALESCE(i.asset_class, 'Cash') AS asset_class,
               COALESCE(i.name, 'Cash - ' || p.position_currency) AS instrument,
               i.isin, p.position_currency, p.quantity, p.market_value_base, p.as_of_date
        FROM position_snapshot p
        JOIN account a ON a.account_id = p.account_id
        LEFT JOIN instrument i ON i.instrument_id = p.instrument_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2 WHERE p2.account_id = p.account_id)
        ORDER BY a.account_label, asset_class, market_value_base DESC
    """).fetchall()

    r = first_row
    for row in rows:
        vals = [row["account_label"], row["asset_class"], row["instrument"], row["isin"],
                row["position_currency"], row["quantity"], row["market_value_base"], None, row["as_of_date"]]
        for i, v in enumerate(vals, start=1):
            if i == 8:
                continue
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = NORMAL
            if i == 6:
                cell.number_format = '#,##0.###'
            if i == 7:
                cell.number_format = CCY_FMT
            cell.border = BORDER
        csgd = ws.cell(row=r, column=8)
        csgd.value = fx_formula(f"$E{r}", f"$G{r}", fx_last_row)
        csgd.font = LINK_FONT
        csgd.number_format = CCY_FMT
        csgd.border = BORDER
        r += 1
    last_row = r - 1

    ws.cell(row=r, column=1, value="TOTAL").font = BOLD
    tc = ws.cell(row=r, column=8, value=f"=SUM(H{first_row}:H{last_row})")
    tc.font = BOLD
    tc.number_format = CCY_FMT

    autosize(ws, [24, 22, 34, 14, 10, 12, 18, 18, 12])
    ws.freeze_panes = "A5"


# ---------------------------------------------------------------------------
def build_fees_sheet(wb, conn, fx_last_row):
    ws = wb.create_sheet("Fees")
    title_block(ws, "Fees (life to date)", "Includes advisory, custody, and transaction fees. SGD column is formula-linked to the FX Rates tab.")

    headers = ["Account", "Date", "Subtype", "Description", "Currency", "Amount (Native)", "Amount (SGD)"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))
    first_row = r + 1

    rows = conn.execute("""
        SELECT a.account_label, t.trade_date, t.txn_subtype, t.raw_txn_type_label,
               t.currency, t.gross_amount
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = 'FEE'
          AND (t.source_leg = 'CASH'
               OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                     SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                       AND t2.txn_type = 'FEE' AND t2.source_leg = 'CASH')))
        ORDER BY a.account_label, t.trade_date
    """).fetchall()

    r = first_row
    for row in rows:
        vals = [row["account_label"], row["trade_date"], row["txn_subtype"],
                row["raw_txn_type_label"], row["currency"], row["gross_amount"], None]
        for i, v in enumerate(vals, start=1):
            if i == 7:
                continue
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = NORMAL
            if i == 6:
                cell.number_format = CCY_FMT
            cell.border = BORDER
        csgd = ws.cell(row=r, column=7)
        csgd.value = fx_formula(f"$E{r}", f"$F{r}", fx_last_row)
        csgd.font = LINK_FONT
        csgd.number_format = CCY_FMT
        csgd.border = BORDER
        r += 1
    last_row = r - 1 if rows else first_row - 1

    ws.cell(row=r, column=1, value="TOTAL").font = BOLD
    if rows:
        tc = ws.cell(row=r, column=7, value=f"=SUM(G{first_row}:G{last_row})")
    else:
        tc = ws.cell(row=r, column=7, value=0)
    tc.font = BOLD
    tc.number_format = CCY_FMT

    autosize(ws, [24, 12, 22, 34, 10, 16, 16])
    ws.freeze_panes = "A5"


# ---------------------------------------------------------------------------
def build_income_sheet(wb, conn, fx_last_row):
    ws = wb.create_sheet("Income")
    title_block(ws, "Income / Dividends (life to date)", "SGD column is formula-linked to the FX Rates tab.")

    headers = ["Account", "Date", "Description", "Currency", "Amount (Native)", "Amount (SGD)"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))
    first_row = r + 1

    rows = conn.execute("""
        SELECT a.account_label, t.trade_date, t.raw_description, t.currency, t.gross_amount
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = 'INCOME'
          AND (t.source_leg = 'CASH'
               OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                     SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                       AND t2.txn_type = 'INCOME' AND t2.source_leg = 'CASH')))
        ORDER BY a.account_label, t.trade_date
    """).fetchall()

    r = first_row
    for row in rows:
        vals = [row["account_label"], row["trade_date"], row["raw_description"],
                row["currency"], row["gross_amount"], None]
        for i, v in enumerate(vals, start=1):
            if i == 6:
                continue
            cell = ws.cell(row=r, column=i, value=v)
            cell.font = NORMAL
            if i == 5:
                cell.number_format = CCY_FMT
            cell.border = BORDER
        csgd = ws.cell(row=r, column=6)
        csgd.value = fx_formula(f"$D{r}", f"$E{r}", fx_last_row)
        csgd.font = LINK_FONT
        csgd.number_format = CCY_FMT
        csgd.border = BORDER
        r += 1
    last_row = r - 1

    ws.cell(row=r, column=1, value="TOTAL").font = BOLD
    tc = ws.cell(row=r, column=6, value=f"=SUM(F{first_row}:F{last_row})")
    tc.font = BOLD
    tc.number_format = CCY_FMT
    total_row = r

    # Summary by account/year using SUMIFS against the data above
    r += 3
    ws.cell(row=r, column=1, value="Summary by Account and Year").font = BOLD
    r += 1
    summary_header_row = r
    for i, h in enumerate(["Account", "Year", "Total (SGD)"], start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, 3)
    r += 1

    combos = conn.execute("""
        SELECT DISTINCT a.account_label, substr(t.trade_date,1,4) AS yr
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type='INCOME'
          AND (t.source_leg = 'CASH'
               OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                     SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                       AND t2.txn_type = 'INCOME' AND t2.source_leg = 'CASH')))
        ORDER BY 1, 2
    """).fetchall()
    for row in combos:
        ws.cell(row=r, column=1, value=row["account_label"]).font = NORMAL
        ws.cell(row=r, column=2, value=row["yr"]).font = NORMAL
        formula = (f'=SUMPRODUCT(($A${first_row}:$A${last_row}=A{r})'
                   f'*(LEFT($B${first_row}:$B${last_row},4)=B{r})'
                   f'*$F${first_row}:$F${last_row})')
        cf = ws.cell(row=r, column=3, value=formula)
        cf.font = NORMAL
        cf.number_format = CCY_FMT
        for col in range(1, 4):
            ws.cell(row=r, column=col).border = BORDER
        r += 1

    autosize(ws, [24, 12, 36, 10, 16, 16])
    ws.freeze_panes = "A5"


# ---------------------------------------------------------------------------
def build_performance_sheet(wb, conn):
    ws = wb.create_sheet("Performance")
    title_block(ws, "Performance (from UBS statement TWR)", "UBS's own linked time-weighted return. Not all accounts have full history yet - see notes column.")

    headers = ["Account", "Currency", "Since Inception TWR", "As Of", "Notes"]
    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, len(headers))
    r += 1
    summary_first_row = r

    accounts = conn.execute("SELECT account_id, account_label FROM account WHERE institution_id='UBS' ORDER BY account_id").fetchall()
    for acct in accounts:
        since_inception = conn.execute(
            """SELECT * FROM account_valuation_history
               WHERE account_id=? AND period_type='cumulative_annual'
               ORDER BY period_end DESC LIMIT 1""", (acct["account_id"],)
        ).fetchone()
        latest = conn.execute(
            """SELECT * FROM account_valuation_history
               WHERE account_id=? ORDER BY period_end DESC LIMIT 1""", (acct["account_id"],)
        ).fetchone()
        ws.cell(row=r, column=1, value=acct["account_label"]).font = NORMAL
        ws.cell(row=r, column=2, value=latest["currency"] if latest else "").font = NORMAL
        if since_inception:
            cv = ws.cell(row=r, column=3, value=since_inception["twr_pct"] / 100)
            cv.number_format = PCT_FMT
            note = ""
        else:
            cv = ws.cell(row=r, column=3, value=None)
            note = "No full since-inception figure available yet (see annual table below)"
        cv.font = NORMAL
        ws.cell(row=r, column=4, value=(since_inception or latest)["period_end"] if latest else "").font = NORMAL
        ws.cell(row=r, column=5, value=note).font = Font(name=FONT_NAME, size=9, italic=True, color="666666")
        for col in range(1, 6):
            ws.cell(row=r, column=col).border = BORDER
        r += 1

    # Annual detail
    r += 2
    ws.cell(row=r, column=1, value="Year-by-Year Detail").font = BOLD
    r += 1
    for i, h in enumerate(["Account", "Year", "Final Value", "Inflows", "Outflows", "TWR"], start=1):
        ws.cell(row=r, column=i, value=h)
    style_header_row(ws, r, 6)
    r += 1

    annual = conn.execute("""
        SELECT a.account_label, h.period_label, h.final_value, h.inflows, h.outflows, h.twr_pct
        FROM account_valuation_history h JOIN account a ON a.account_id = h.account_id
        WHERE h.period_type='year_end'
        ORDER BY a.account_label, h.period_end
    """).fetchall()
    for row in annual:
        ws.cell(row=r, column=1, value=row["account_label"]).font = NORMAL
        ws.cell(row=r, column=2, value=row["period_label"]).font = NORMAL
        for col, key in ((3, "final_value"), (4, "inflows"), (5, "outflows")):
            c = ws.cell(row=r, column=col, value=row[key])
            c.font = NORMAL
            c.number_format = CCY_FMT
        c = ws.cell(row=r, column=6, value=(row["twr_pct"] or 0) / 100)
        c.font = NORMAL
        c.number_format = PCT_FMT
        for col in range(1, 7):
            ws.cell(row=r, column=col).border = BORDER
        r += 1

    autosize(ws, [24, 12, 16, 16, 16, 20])
    ws.freeze_panes = f"A{summary_first_row}"


# ---------------------------------------------------------------------------
def main(output_path=OUTPUT_DEFAULT):
    conn = get_connection()
    wb = Workbook()
    wb.remove(wb.active)

    as_of = conn.execute("SELECT max(as_of_date) FROM position_snapshot").fetchone()[0]

    fx_last_row = build_fx_sheet(wb, conn)
    build_overview_sheet(wb, conn, fx_last_row, as_of)
    build_positions_sheet(wb, conn, fx_last_row)
    build_fees_sheet(wb, conn, fx_last_row)
    build_income_sheet(wb, conn, fx_last_row)
    build_performance_sheet(wb, conn)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    conn.close()
    print(f"Saved {output_path}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_DEFAULT
    main(out)
