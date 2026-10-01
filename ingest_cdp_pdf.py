"""
Parses a CDP (Central Depository) monthly Account Statement PDF into
positions and transactions. Unlike Endowus, this PDF has a real text layer
(pdfplumber text extraction works directly, no OCR needed), so it's the
simplest of the three institution parsers built so far.

What's in this statement:
  - Securities Holdings (SGD and USD sub-sections) - equities/REITs, with
    quantity and market value but no ISIN and no cost basis (CDP is a
    depository, not a broker - trades happen through linked brokers, e.g.
    iFAST/Phillip Securities, not visible here).
  - Bonds (SGD and USD) - SGD Savings Bonds show market value as "NA" (not
    exchange-traded), so we fall back to face value for those specifically.
  - Securities On Loan - quantity lent out under Securities Borrowing &
    Lending (SBL). NOT included in the statement's own Total Balance, so
    this needs to be ADDED on top of holdings, not treated as already
    counted (confirmed against the statement's own arithmetic - see
    load_cdp_statement()'s reconciliation gate).
  - Cash Transaction - dividends and SBL lending fees arrive as income,
    then get paid out to an external bank (DBS, per user confirmation - not
    one of our other tracked accounts) the same or next business day, so
    the cash balance is always ~zero. Tagged WITHDRAWAL, not TRANSFER.

As with Endowus, load_cdp_statement() gates the load behind reconciliation
against the statement's own stated totals - don't relax that.
"""
import re
from pathlib import Path

import pdfplumber

from db import is_source_file_loaded

NUM = r"[\d,]+(?:\.\d+)?"


def _to_float(s):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if s in ("", "NA", "NIL", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_iso_date(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{y}-{mth}-{d}"


def parse_statement(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    result = {
        "source_file": Path(pdf_path).name,
        "period_label": None, "as_of_date": None, "account_suffix": None,
        "summary": {}, "holdings": [], "bonds": [], "on_loan": [],
        "cash_transactions": [], "usd_sgd_rate": None,
    }

    m = re.search(r"SECURITIES A/C NO\.\s*XXXX-XXXX-(\d{4})", text)
    if m:
        result["account_suffix"] = m.group(1)

    m = re.search(r"([A-Z]{3}) (\d{4})\s*PAGE 1/", text)
    if m:
        mon_map = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                   "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        mon, year = m.group(1), m.group(2)
        result["period_label"] = f"{mon} {year}"
        if mon in mon_map:
            # statement covers the calendar month; use month-end as as-of date
            import calendar
            last_day = calendar.monthrange(int(year), mon_map[mon])[1]
            result["as_of_date"] = f"{year}-{mon_map[mon]:02d}-{last_day:02d}"

    m = re.search(rf"Main Balance\s+SGD\s+({NUM})\s+USD\s+({NUM})", text)
    if m:
        result["summary"]["main_sgd"] = _to_float(m.group(1))
        result["summary"]["main_usd"] = _to_float(m.group(2))
    m = re.search(rf"Total Balance\s+SGD\s+({NUM})\s+USD\s+({NUM})\s*\n\s*\(Equiv\. SGD:\s*({NUM})\)", text)
    if m:
        result["summary"]["total_sgd"] = _to_float(m.group(1))
        result["summary"]["total_usd"] = _to_float(m.group(2))
        result["summary"]["total_usd_equiv_sgd"] = _to_float(m.group(3))
        if result["summary"]["total_usd"]:
            result["usd_sgd_rate"] = result["summary"]["total_usd_equiv_sgd"] / result["summary"]["total_usd"]

    lines = text.split("\n")
    section, currency = None, "SGD"
    holding_re = re.compile(rf"^(.+?)\s+({NUM})\s+NIL\s+({NUM})\s+({NUM})\s+({NUM})$")
    bond_re = re.compile(rf"^(.+?)\s+({NUM})\s+NIL\s+({NUM})\s+(NA|{NUM})\s+(NA|{NUM})\s+({NUM})$")
    loan_re = re.compile(rf"^(.+?)\s+(\d{{2}}/\d{{2}}/\d{{4}})\s+({NUM})\s+({NUM})\s+({NUM})\s+({NUM})$")
    cash_re = re.compile(rf"^(\d{{2}}/\d{{2}}/\d{{4}})\s+(.+?)\s+(-?{NUM})$")

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Securities Holdings"):
            section, currency = "HOLDINGS", "SGD"
            continue
        if line.startswith("Bonds"):
            section, currency = "BONDS", "SGD"
            continue
        if line.startswith("Securities On Loan"):
            section = "LOAN"
            continue
        if line.startswith("Securities Transaction"):
            section = None
            continue
        if line.startswith("Cash Transaction"):
            section = "CASH"
            continue
        if line.startswith("Your Securities Account"):
            section = None
            continue
        if line == "USD" or line.startswith("USD USD"):
            currency = "USD"
            continue
        if line.startswith("TOTAL:") or line.startswith("SGD SGD") or line.startswith("Security "):
            continue

        if section == "HOLDINGS":
            m = holding_re.match(line)
            if m:
                name, free, bal, price, mv = m.groups()
                result["holdings"].append({
                    "name": name.strip(), "currency": currency,
                    "quantity": _to_float(bal), "price": _to_float(price),
                    "market_value": _to_float(mv),
                })
        elif section == "BONDS":
            m = bond_re.match(line)
            if m:
                name, free, bal, price, mv, face = m.groups()
                result["bonds"].append({
                    "name": name.strip(), "currency": currency,
                    "quantity": _to_float(bal), "price": _to_float(price),
                    "market_value": _to_float(mv), "face_value": _to_float(face),
                })
        elif section == "LOAN":
            m = loan_re.match(line)
            if m:
                name, eff_date, rate, qty, price, mv = m.groups()
                result["on_loan"].append({
                    "name": name.strip(), "effective_date": _to_iso_date(eff_date),
                    "rate_pct": _to_float(rate), "quantity": _to_float(qty),
                    "price": _to_float(price), "market_value": _to_float(mv),
                })
        elif section == "CASH":
            if "Balance B/F" in line or "Balance C/F" in line:
                m = re.search(rf"(\d{{2}}/\d{{2}}/\d{{4}}).*?(-?{NUM})$", line)
                if m:
                    key = "cash_bf" if "B/F" in line else "cash_cf"
                    result["summary"][key] = _to_float(m.group(2))
                continue
            m = cash_re.match(line)
            if m:
                trade_date, desc, amount = m.groups()
                result["cash_transactions"].append({
                    "trade_date": _to_iso_date(trade_date),
                    "description": desc.strip(), "amount": _to_float(amount),
                })

    return result


# ---------------------------------------------------------------------------
def load_cdp_statement(pdf_path, conn, account_native_code=None, force=False):
    source_file = Path(pdf_path).name
    if not force and is_source_file_loaded(conn, "position_snapshot", source_file):
        print(f"[cdp] SKIPPED: '{source_file}' already loaded (use --force to reload anyway)")
        return None

    parsed = parse_statement(pdf_path)
    source_file = parsed["source_file"]
    summ = parsed["summary"]

    # Resolve which CDP account this is from the statement's OWN printed
    # account number (e.g. "XXXX-XXXX-9563"), unless the caller explicitly
    # overrode it - avoids needing a CLI flag to say which of several CDP
    # accounts a given file belongs to, and is safer than defaulting to one.
    if account_native_code is None:
        if not parsed["account_suffix"]:
            print(f"[cdp] ABORT: could not read the account number from {source_file}'s own "
                  f"text, and no account_native_code override was given.")
            return None
        account_native_code = f"CDP {parsed['account_suffix']}"

    # --- Reconciliation gate ---
    checks = []
    sgd_hold = round(sum(h["market_value"] for h in parsed["holdings"] if h["currency"] == "SGD"), 2)
    usd_hold = round(sum(h["market_value"] for h in parsed["holdings"] if h["currency"] == "USD"), 2)
    usd_bond = round(sum(b["market_value"] or 0 for b in parsed["bonds"] if b["currency"] == "USD"), 2)
    on_loan_total = round(sum(l["market_value"] for l in parsed["on_loan"]), 2)
    cash_net = round((summ.get("cash_bf") or 0) + sum(t["amount"] for t in parsed["cash_transactions"]), 2)

    checks.append(("SGD holdings vs Main Balance SGD", sgd_hold, summ.get("main_sgd")))
    checks.append(("USD holdings+bond vs Total Balance USD", round(usd_hold + usd_bond, 2), summ.get("total_usd")))
    checks.append(("Cash B/F+flows vs Cash C/F", cash_net, summ.get("cash_cf")))

    all_ok = True
    for label, computed, stated in checks:
        if stated is None or computed is None or abs(computed - stated) > 0.02:
            print(f"[cdp] ABORT: reconciliation failed - {label}: computed={computed}, stated={stated}")
            all_ok = False
    if not all_ok:
        print(f"[cdp] Not loading {source_file} - check OCR/parsing output before retrying.")
        return None
    for label, computed, stated in checks:
        print(f"[cdp] Reconciliation OK - {label}: {computed} matches {stated}")

    account_id = conn.execute(
        "SELECT account_id FROM account WHERE institution_id='CDP' AND native_account_code=?",
        (account_native_code,),
    ).fetchone()
    if not account_id:
        print(f"[cdp] ABORT: no account configured for '{account_native_code}' - add it to config/accounts.csv first.")
        return None
    account_id = account_id["account_id"]

    for label, computed, stated in checks:
        conn.execute(
            """INSERT INTO reconciliation_log
                 (account_id, scope, period_end, expected_value, actual_value, difference, status, notes)
               VALUES (?, 'CDP_STATEMENT_RECONCILIATION', ?, ?, ?, ?, 'OK', ?)""",
            (account_id, parsed["as_of_date"], stated, computed, round(computed - stated, 2),
             f"{label} (source: {source_file})"),
        )

    on_loan_by_name = {l["name"]: l for l in parsed["on_loan"]}

    n_positions = 0
    conn.execute(
        "DELETE FROM position_snapshot WHERE account_id=? AND as_of_date=?",
        (account_id, parsed["as_of_date"]),
    )
    for h in parsed["holdings"]:
        instrument_id = f"CDP:{h['name']}"
        conn.execute(
            """INSERT INTO instrument (instrument_id, name, asset_class, instrument_currency)
               VALUES (?, ?, 'Equities', ?)
               ON CONFLICT(instrument_id) DO UPDATE SET
                 asset_class=COALESCE(instrument.asset_class, excluded.asset_class)""",
            (instrument_id, h["name"], h["currency"]),
        )
        loan = on_loan_by_name.get(h["name"])
        qty_on_loan = loan["quantity"] if loan else None
        total_qty = h["quantity"] + (qty_on_loan or 0)
        total_mv = h["market_value"] + (loan["market_value"] if loan else 0)
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, quantity_on_loan, market_price, market_value_base, source_file)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, parsed["as_of_date"], h["currency"],
             total_qty, qty_on_loan, h["price"], total_mv, source_file),
        )
        n_positions += 1

    for b in parsed["bonds"]:
        instrument_id = f"CDP:{b['name']}"
        conn.execute(
            """INSERT INTO instrument (instrument_id, name, asset_class, instrument_currency)
               VALUES (?, ?, 'Bonds', ?)
               ON CONFLICT(instrument_id) DO UPDATE SET
                 asset_class=COALESCE(instrument.asset_class, excluded.asset_class)""",
            (instrument_id, b["name"], b["currency"]),
        )
        # SGD Savings Bonds show market value as NA (not exchange-traded) -
        # fall back to face value, which is the best available valuation.
        mv = b["market_value"] if b["market_value"] is not None else b["face_value"]
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, market_price, market_value_base, source_file)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, parsed["as_of_date"], b["currency"],
             b["quantity"], b["price"], mv, source_file),
        )
        n_positions += 1
        if b["market_value"] is None:
            print(f"[cdp] NOTE: {b['name']} valued at face ({b['face_value']:,.2f}) - "
                  f"not exchange-traded, no market price available.")

    n_txn = 0
    for i, t in enumerate(parsed["cash_transactions"]):
        desc = t["description"]
        if desc.startswith("Payment Made"):
            txn_type, subtype = "WITHDRAWAL", "PAYOUT_TO_DBS"
        elif "Dividend" in desc:
            txn_type, subtype = "INCOME", "DIVIDEND"
        elif "Interest Payment" in desc:
            txn_type, subtype = "INCOME", "BOND_INTEREST"
        elif "Lending Fee" in desc:
            txn_type, subtype = "INCOME", "SBL_LENDING_FEE"
        else:
            txn_type, subtype = "OTHER", "UNCLASSIFIED"
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, gross_amount, raw_description, source_reference, source_file)
               VALUES (?, NULL, 'CASH', ?, ?, ?, 'SGD', ?, ?, ?, ?)""",
            (account_id, t["trade_date"], txn_type, subtype, t["amount"], desc,
             f"{source_file}:CASH:{i}", source_file),
        )
        n_txn += cur.rowcount

    conn.commit()
    print(f"[cdp] {source_file}: loaded {n_positions} positions ({len(parsed['on_loan'])} on loan), "
          f"{n_txn} cash txns")
    return {"n_positions": n_positions, "n_txn": n_txn}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[2] == "--load":
        from db import get_connection
        conn = get_connection()
        force = "--force" in sys.argv
        load_cdp_statement(sys.argv[1], conn, force=force)
        conn.close()
    else:
        import json
        r = parse_statement(sys.argv[1])
        print(json.dumps({k: v for k, v in r.items()}, indent=2, default=str))
