"""
Parses an Interactive Brokers "Activity Statement" PDF. Real text layer (no
OCR needed). One PDF can cover multiple accounts (this user has 2); each
gets its own self-contained section with an identical layout, found by
locating "Account Information" occurrences.

What's here, per account:
  - NAV table + "Change in NAV" walk (Starting/Mark-to-Market/Deposits &
    Withdrawals/Position Transfers/interest/commissions/tax/FX -> Ending) -
    this is the reconciliation gate, and also gives us IBKR's own TWR for
    the period directly.
  - Cash Report (per currency): CATEGORY TOTALS for the period (Commissions,
    Deposits, Account Transfers, Trades (Sales), Sales Tax, Cash FX
    Translation), not one row per event like UBS/CDP. Loaded as one CASH-leg
    txn per category per currency.
  - Open Positions + Forex Balances -> position_snapshot (stocks + cash by
    currency, the same "cash by currency" pattern as UBS).
  - Trades -> SECURITY-leg txns with real quantity/price/commission/realized P/L.
  - Transfers -> SECURITY-leg TRANSFER txns. "Internal" transfers name the
    other IBKR account directly (no fuzzy pairing needed, unlike UBS);
    "FOP" transfers are external (a different custodian).
  - Deposits & Withdrawals -> distinguishes real external flows ("Electronic
    Fund Transfer") from inter-account moves ("Internal Transfer To/From
    Account UXXXXXXXX") - the account number is given directly, so these can
    be resolved and auto-paired exactly, not detected by amount/date matching.

Reconciliation gate: the Change in NAV walk must sum to the stated Ending
Value - refuses to load otherwise, same pattern as Endowus/CDP.
"""
import re
from pathlib import Path

import pdfplumber

NUM = r"-?[\d,]+\.\d{2}"


def _f(s):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if s in ("", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _account_sections(full_text_pages):
    """Returns [(account_code, joined_text_for_that_account)]."""
    starts = []
    for i, t in enumerate(full_text_pages):
        m = re.search(r"Account\s+(U\d+)\s*\n", t)
        if m and "Account Information" in t:
            starts.append((i, m.group(1)))
    sections = []
    for idx, (page_i, code) in enumerate(starts):
        end_page = starts[idx + 1][0] if idx + 1 < len(starts) else len(full_text_pages)
        sections.append((code, "\n".join(full_text_pages[page_i:end_page])))
    return sections


def _parse_nav_walk(text):
    out = {}
    m = re.search(rf"Time Weighted Rate of Return\s+(-?[\d.]+)%", text)
    if m:
        out["twr_pct"] = float(m.group(1))
    fields = ["Starting Value", "Mark-to-Market", "Deposits & Withdrawals", "Position Transfers",
              "Change in Interest Accruals", "Commissions", "Sales Tax", "Other FX Translations", "Ending Value"]
    for f in fields:
        m = re.search(rf"{re.escape(f)}\s+({NUM})", text)
        if m:
            out[f] = _f(m.group(1))
    return out


def _parse_open_positions(text):
    rows = []
    m = re.search(r"Open Positions\n(.*?)(?:\nForex Balances|\nNet Stock Position Summary)", text, re.S)
    if not m:
        return rows
    block = m.group(1)
    currency = "SGD"
    row_re = re.compile(
        rf"^([A-Z][A-Z0-9.]*)\s+([\d,]+)\s+(\d+)\s+([\d.]+)\s+({NUM})\s+([\d.]+)\s+({NUM})\s+({NUM})")
    for line in block.split("\n"):
        s = line.strip()
        if s in ("Stocks",):
            continue
        if re.match(r"^[A-Z]{3}$", s):
            currency = s
            continue
        m2 = row_re.match(s)
        if m2:
            sym, qty, mult, cost_price, cost_basis, close_price, value, unreal = m2.groups()
            rows.append({
                "symbol": sym, "currency": currency, "quantity": _f(qty),
                "cost_price": _f(cost_price), "cost_basis": _f(cost_basis),
                "close_price": _f(close_price), "value": _f(value),
            })
    return rows


def _parse_forex_balances(text):
    rows = []
    m = re.search(r"Forex Balances\n(.*?)\n(?:Net Stock Position Summary|Trades\n)", text, re.S)
    if not m:
        return rows
    block = m.group(1)
    row_re = re.compile(rf"^([A-Z]{{3}})\s+({NUM})\s+([\d.]+)\s+({NUM})\s+([\d.]+)\s+({NUM})\s+({NUM})")
    for line in block.split("\n"):
        m2 = row_re.match(line.strip())
        if m2:
            ccy, qty, cost_price, cost_basis_sgd, close_price, value_sgd, unreal_sgd = m2.groups()
            rows.append({
                "currency": ccy, "quantity": _f(qty), "close_price": _f(close_price),
                "value_sgd": _f(value_sgd),
            })
    return rows


def _parse_trades(text):
    rows = []
    m = re.search(r"\nTrades\n(.*?)\nTransaction Fees", text, re.S)
    if not m:
        return rows
    block = m.group(1)
    currency = "SGD"
    date_buffer = None
    row_re = re.compile(rf"^([A-Z][A-Z0-9.]*)\s+(-?[\d,]+)\s+([\d.]+)\s+([\d.]+)\s+"
                         rf"({NUM})\s+({NUM})\s+({NUM})\s+({NUM})\s+({NUM})")
    for raw in block.split("\n"):
        s = raw.strip()
        if not s or s.startswith("Symbol") or s.startswith("Total") or s == "Stocks":
            continue
        if re.match(r"^[A-Z]{3}$", s):
            currency = s
            continue
        dm = re.match(r"^(\d{4}-\d{2}-\d{2}),\s*(\d{2}:\d{2}:\d{2})?$", s)
        if dm:
            date_buffer = (dm.group(1), dm.group(2))
            continue
        m2 = row_re.match(s)
        if m2:
            sym, qty, tprice, cprice, proceeds, comm, basis, realized, mtm = m2.groups()
            time_part = date_buffer[1] if date_buffer else None
            trade_date = date_buffer[0] if date_buffer else None
            if not time_part:
                dm2 = re.match(r"^(\d{4}-\d{2}-\d{2}),\s*(\d{2}:\d{2}:\d{2})\s+" + re.escape(sym), s)
            rows.append({
                "symbol": sym, "currency": currency, "trade_date": trade_date,
                "quantity": _f(qty), "trade_price": _f(tprice), "proceeds": _f(proceeds),
                "commission": _f(comm), "realized_pl": _f(realized),
            })
            date_buffer = None
    return rows


def _parse_transfers(text):
    rows = []
    m = re.search(r"\nTransfers\n(.*?)\n(?:Interest Accruals|Deposits & Withdrawals)", text, re.S)
    if not m:
        return rows
    block = m.group(1)
    currency = "SGD"
    row_re = re.compile(
        rf"^([A-Z][A-Z0-9.]*)\s+(\d{{4}}-\d{{2}}-\d{{2}})\s+(FOP|Internal)\s+(In|Out)\s+"
        rf"(\S+|--)\s+(\S+|--)\s+(-?[\d,]+)\s+(\S+|--)\s+({NUM})")
    for line in block.split("\n"):
        s = line.strip()
        if re.match(r"^[A-Z]{3}$", s):
            currency = s
            continue
        m2 = row_re.match(s)
        if m2:
            sym, date, ttype, direction, xfer_co, xfer_acct, qty, xfer_price, mv = m2.groups()
            rows.append({
                "symbol": sym, "currency": currency, "trade_date": date,
                "transfer_type": ttype, "direction": direction,
                "xfer_account": xfer_acct if xfer_acct != "--" else None,
                "quantity": _f(qty), "market_value": _f(mv),
            })
    return rows


def _parse_cash_report(text):
    """Returns {currency: {category: amount}}."""
    out = {}
    m = re.search(r"\nCash Report\n(.*?)\n(?:Open Positions|Forex Balances|Net Stock Position Summary)", text, re.S)
    if not m:
        return out
    block = m.group(1)
    currency = "SGD"  # base currency summary comes first
    categories = ["Commissions", "Deposits", "Account Transfers", "Trades \\(Sales\\)",
                  "Sales Tax", "Cash FX Translation Gain/Loss"]
    for line in block.split("\n"):
        s = line.strip()
        if re.match(r"^[A-Z]{3}$", s) and s not in ("Cash",):
            currency = s
            out.setdefault(currency, {})
            continue
        for cat in categories:
            m2 = re.match(rf"^{cat}\s+({NUM})(?:\s+{NUM})?", s)
            if m2:
                out.setdefault(currency, {})[cat.replace("\\(Sales\\)", "(Sales)")] = _f(m2.group(1))
    return out


def _parse_deposits_withdrawals(text):
    rows = []
    m = re.search(r"Deposits & Withdrawals\n(.*?)\nGST Details", text, re.S)
    if not m:
        return rows
    block = m.group(1)
    currency = "SGD"
    for line in block.split("\n"):
        s = line.strip()
        if re.match(r"^[A-Z]{3}$", s):
            currency = s
            continue
        m2 = re.search(rf"(\d{{4}}-\d{{2}}-\d{{2}})\s+(.+?)\s+({NUM})$", s)
        if m2:
            date, desc, amount = m2.groups()
            other_acct = None
            am = re.search(r"Account (U\d+)", desc)
            if am:
                other_acct = am.group(1)
            rows.append({"trade_date": date, "description": desc.strip(),
                         "currency": currency, "amount": _f(amount),
                         "counterparty_account": other_acct})
    return rows


def _fix_deposit_currencies(deposits_withdrawals, cash_report):
    """The Deposits & Withdrawals section sits in a two-column page layout
    next to Interest Accruals, and text extraction interleaves the two -
    the currency header can end up merged onto an unrelated Interest
    Accruals line, making it unreliable to read directly. Cross-referencing
    each entry's amount against the Cash Report (which IS single-column and
    parses cleanly) is more robust than parsing the raw layout."""
    for d in deposits_withdrawals:
        for ccy, cats in cash_report.items():
            for cat in ("Deposits", "Account Transfers"):
                val = cats.get(cat)
                if val is not None and abs(val - d["amount"]) < 0.01:
                    d["currency"] = ccy
                    break
            else:
                continue
            break
    return deposits_withdrawals


def parse_statement(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]

    accounts = {}
    for code, text in _account_sections(pages):
        cash_report = _parse_cash_report(text)
        deposits_withdrawals = _fix_deposit_currencies(_parse_deposits_withdrawals(text), cash_report)
        accounts[code] = {
            "nav_walk": _parse_nav_walk(text),
            "open_positions": _parse_open_positions(text),
            "forex_balances": _parse_forex_balances(text),
            "trades": _parse_trades(text),
            "transfers": _parse_transfers(text),
            "cash_report": cash_report,
            "deposits_withdrawals": deposits_withdrawals,
        }

    m = re.search(r"([A-Za-z]+ \d{1,2}, \d{4}) - ([A-Za-z]+ \d{1,2}, \d{4})", pages[0])
    period = {"start": None, "end": None}
    if m:
        from datetime import datetime
        period["start"] = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        period["end"] = datetime.strptime(m.group(2), "%B %d, %Y").date().isoformat()

    return {"accounts": accounts, "period": period, "source_file": Path(pdf_path).name}


if __name__ == "__main__":
    import sys
    import json
    r = parse_statement(sys.argv[1])
    print(json.dumps(r, indent=2, default=str))
