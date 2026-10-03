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

from db import is_source_file_loaded

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
    # "Interest" is a genuinely separate, necessary term in the NAV walk
    # identity - confirmed by hand (the walk doesn't close without it,
    # found on a real August statement where it appeared for the first
    # time). It's NOT the same as "Change in Interest Accruals" (handled
    # above) or the "Interest Accruals" breakdown row in the NAV table -
    # the negative lookahead excludes both of those explicitly.
    m = re.search(rf"Interest(?!\s+Accruals)\s+({NUM})", text)
    if m:
        out["Interest"] = _f(m.group(1))
    m = re.search(rf"Interest Accruals\s+{NUM}\s+{NUM}\s+{NUM}\s+({NUM})\s+{NUM}", text)
    if m:
        out["interest_accruals_ending"] = _f(m.group(1))
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
    # Bound on the table's OWN closing "Total" line, not on whatever
    # section happens to follow - the previous approach (guessing "Net
    # Stock Position Summary" or "Trades\n" would always come next) broke
    # on a real statement where an account had neither section that month
    # (no trades at all), so the whole match silently failed and returned
    # nothing. The Forex Balances table always has its own "Total ..."
    # closing row regardless of what comes after it, confirmed against
    # both the July and August statements.
    m = re.search(r"Forex Balances\n(.*?)\nTotal\s", text, re.S)
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


# ---------------------------------------------------------------------------
def _fx_rate_to_sgd(currency, forex_balances):
    if currency == "SGD":
        return 1.0
    for fb in forex_balances:
        if fb["currency"] == currency:
            return fb["close_price"]
    return None


def load_ibkr_statement(pdf_path, conn, account_native_codes=None, force=False):
    """Loads every account found in the PDF (or just account_native_codes,
    if given). Returns {account_code: result_dict or None-if-aborted}."""
    source_file = Path(pdf_path).name
    if not force and is_source_file_loaded(conn, "position_snapshot", source_file):
        print(f"[ibkr] SKIPPED: '{source_file}' already loaded (use --force to reload anyway)")
        return {}

    parsed = parse_statement(pdf_path)
    source_file = parsed["source_file"]
    results = {}

    for code, data in parsed["accounts"].items():
        if account_native_codes and code not in account_native_codes:
            continue
        results[code] = _load_one_ibkr_account(conn, code, data, source_file, parsed["period"])
    return results


def _load_one_ibkr_account(conn, native_code, data, source_file, period):
    nav = data["nav_walk"]

    # --- Reconciliation gate: the NAV walk must sum to the stated ending
    # value. This is IBKR's own accounting identity, not ours - refuse to
    # load if it doesn't hold.
    #
    # IBKR omits a line entirely when it's zero for the period, rather than
    # printing "0.00" - confirmed on a real statement where Deposits &
    # Withdrawals, Position Transfers, Commissions, and Sales Tax were all
    # absent in a quiet month with no corresponding activity. So a missing
    # OPTIONAL component defaults to 0 rather than aborting - only
    # "Starting Value" and "Ending Value" are true anchors the identity
    # can't do without, so those two stay required.
    #
    # "Interest" (distinct from "Change in Interest Accruals") is also
    # included here - found on the same statement, confirmed by hand that
    # the walk doesn't close without it. ---
    walk_fields = ["Mark-to-Market", "Deposits & Withdrawals", "Position Transfers",
                   "Change in Interest Accruals", "Commissions", "Sales Tax",
                   "Other FX Translations", "Interest"]
    if "Starting Value" not in nav or "Ending Value" not in nav:
        print(f"[ibkr] ABORT {native_code}: NAV walk missing Starting or Ending Value - {nav}")
        return None
    computed_ending = round(nav["Starting Value"] + sum(nav.get(f, 0.0) for f in walk_fields), 2)
    stated_ending = round(nav["Ending Value"], 2)
    if abs(computed_ending - stated_ending) > 0.02:
        print(f"[ibkr] ABORT {native_code}: NAV walk does not reconcile "
              f"(computed={computed_ending}, stated={stated_ending}) - not loading.")
        return None
    print(f"[ibkr] Reconciliation OK {native_code}: NAV walk sums to {computed_ending} "
          f"(matches stated {stated_ending})")

    account_id = conn.execute(
        "SELECT account_id FROM account WHERE institution_id='IBKR' AND native_account_code=?",
        (native_code,),
    ).fetchone()
    if not account_id:
        print(f"[ibkr] ABORT {native_code}: no account configured - add it to config/accounts.csv first.")
        return None
    account_id = account_id["account_id"]

    conn.execute(
        """INSERT INTO reconciliation_log
             (account_id, scope, period_start, period_end, expected_value, actual_value, difference, status, notes)
           VALUES (?, 'IBKR_NAV_WALK', ?, ?, ?, ?, ?, 'OK', ?)""",
        (account_id, period.get("start"), period.get("end"), stated_ending, computed_ending,
         round(computed_ending - stated_ending, 2), f"NAV walk reconciles (source: {source_file})"),
    )

    # Persist IBKR's own TWR%, same pattern Endowus already uses - this was
    # previously parsed and printed once, then discarded, so the Performance
    # page had nothing to show for IBKR despite the figure already existing
    # in every statement. IBKR's NAV walk values are already in SGD (the
    # account's base currency, confirmed by matching our own independently-
    # computed SGD totals), so no FX conversion is needed here.
    inflows = sum(d["amount"] for d in data["deposits_withdrawals"] if d["amount"] > 0)
    outflows = sum(d["amount"] for d in data["deposits_withdrawals"] if d["amount"] < 0)
    gain_value = (nav.get("Mark-to-Market", 0) + nav.get("Interest", 0)
                  + nav.get("Change in Interest Accruals", 0) + nav.get("Other FX Translations", 0))
    conn.execute(
        """INSERT OR IGNORE INTO account_valuation_history
             (account_id, period_type, period_end, period_label, currency,
              final_value, inflows, outflows, gain_value, twr_pct, source_file)
           VALUES (?, 'month_end', ?, ?, 'SGD', ?, ?, ?, ?, ?, ?)""",
        (account_id, period.get("end"), f"{period.get('start')} to {period.get('end')}",
         stated_ending, inflows, outflows, gain_value, nav.get("twr_pct"), source_file),
    )

    as_of_date = period.get("end")
    n_positions = 0
    conn.execute(
        "DELETE FROM position_snapshot WHERE account_id=? AND as_of_date=?",
        (account_id, as_of_date),
    )

    # Stock positions - value is in native currency; convert to SGD via the
    # matching currency's FX rate from Forex Balances (that rate is IBKR's
    # own, so this stays consistent with how they valued everything else).
    for p in data["open_positions"]:
        instrument_id = f"IBKR:{p['symbol']}"
        conn.execute(
            """INSERT INTO instrument (instrument_id, name, asset_class, instrument_currency)
               VALUES (?, ?, 'Equities', ?)
               ON CONFLICT(instrument_id) DO UPDATE SET
                 asset_class=COALESCE(instrument.asset_class, excluded.asset_class)""",
            (instrument_id, p["symbol"], p["currency"]),
        )
        rate = _fx_rate_to_sgd(p["currency"], data["forex_balances"])
        mv_sgd = p["value"] * rate if rate else None
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, cost_price, market_price, market_value_native, market_value_base, source_file)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, as_of_date, p["currency"], p["quantity"],
             p["cost_price"], p["close_price"], p["value"], mv_sgd, source_file),
        )
        n_positions += 1

    # Cash by currency (Forex Balances) - value_sgd is already given directly,
    # no conversion needed, and it's the more precise source for cash.
    for fb in data["forex_balances"]:
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, market_price, market_value_base, source_file)
               VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?)""",
            (account_id, as_of_date, fb["currency"], fb["quantity"],
             fb["close_price"], fb["value_sgd"], source_file),
        )
        n_positions += 1

    # Interest accruals - a real component of NAV (Cash + Stock + Interest
    # Accruals = Total per IBKR's own table) that isn't in either Open
    # Positions or Forex Balances - load it as its own cash-like row so the
    # account's total position value actually matches the statement's NAV.
    if nav.get("interest_accruals_ending"):
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  market_value_base, source_file)
               VALUES (?, NULL, 1, ?, 'SGD', ?, ?)""",
            (account_id, as_of_date, nav["interest_accruals_ending"], source_file),
        )
        n_positions += 1

    # A forced reload REPLACES this statement's transactions rather than adding to them (same
    # pattern as positions). INSERT OR IGNORE cannot do this job: cash rows have no instrument or
    # quantity, SQLite treats NULLs as distinct in a UNIQUE constraint, so it never fires and a
    # forced reload used to store every row a second time.
    conn.execute("DELETE FROM txn WHERE account_id=? AND source_file=?", (account_id, source_file))

    # SECURITY leg: trades
    n_sec = 0
    for i, t in enumerate(data["trades"]):
        instrument_id = f"IBKR:{t['symbol']}"
        txn_type = "SELL" if t["quantity"] < 0 else "BUY"
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type,
                  currency, quantity, price, gross_amount, fee_amount, realized_pl,
                  raw_txn_type_label, source_reference, source_file)
               VALUES (?, ?, 'SECURITY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, t["trade_date"], txn_type, t["currency"],
             t["quantity"], t["trade_price"], t["proceeds"], t["commission"], t["realized_pl"],
             "Trade", f"{source_file}:{native_code}:TRADE:{i}", source_file),
        )
        n_sec += cur.rowcount

    # SECURITY leg: security transfers (in/out of the account, not cash)
    for i, t in enumerate(data["transfers"]):
        instrument_id = f"IBKR:{t['symbol']}"
        if t["transfer_type"] == "Internal":
            subtype = f"INTERNAL_TRANSFER_{t['direction'].upper()}_{t['xfer_account']}"
        else:
            subtype = f"EXTERNAL_SECURITIES_TRANSFER_{t['direction'].upper()}"
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, quantity, gross_amount, raw_txn_type_label, raw_description,
                  source_reference, source_file)
               VALUES (?, ?, 'SECURITY', ?, 'TRANSFER', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, t["trade_date"], subtype, t["currency"],
             t["quantity"], t["market_value"], f"{t['transfer_type']} {t['direction']}",
             f"Xfer account/company: {t['xfer_account']}",
             f"{source_file}:{native_code}:XFER:{i}", source_file),
        )
        n_sec += cur.rowcount

    # CASH leg: real dated deposits/withdrawals/internal-transfers
    n_cash = 0
    for i, d in enumerate(data["deposits_withdrawals"]):
        if d["counterparty_account"]:
            txn_type, subtype = "TRANSFER", f"INTERNAL_TRANSFER_TO_{d['counterparty_account']}"
        else:
            txn_type, subtype = ("DEPOSIT", "EXTERNAL_INCOMING") if d["amount"] > 0 else \
                                 ("WITHDRAWAL", "EXTERNAL_OUTGOING")
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, gross_amount, raw_description, source_reference, source_file)
               VALUES (?, NULL, 'CASH', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, d["trade_date"], txn_type, subtype, d["currency"], d["amount"],
             d["description"], f"{source_file}:{native_code}:DW:{i}", source_file),
        )
        n_cash += cur.rowcount

    # CASH leg: fee/tax/fx categories from Cash Report (aggregated - IBKR
    # gives period totals here, not per-event detail). Deliberately skip
    # "Deposits"/"Account Transfers" (already loaded, dated, above) and
    # "Trades (Sales)" (already reflected in the SECURITY-leg trade proceeds).
    cat_map = {
        "Commissions": ("FEE", "IBKR_COMMISSION"),
        "Sales Tax": ("FEE", "GST_SALES_TAX"),
        "Cash FX Translation Gain/Loss": ("OTHER", "FX_TRANSLATION"),
    }
    for currency, cats in data["cash_report"].items():
        for cat, amount in cats.items():
            if cat not in cat_map or amount is None:
                continue
            txn_type, subtype = cat_map[cat]
            cur = conn.execute(
                """INSERT OR IGNORE INTO txn
                     (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                      currency, gross_amount, raw_description, source_reference, source_file)
                   VALUES (?, NULL, 'CASH', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, as_of_date, txn_type, subtype, currency, amount,
                 f"{cat} (period total)", f"{source_file}:{native_code}:CASHCAT:{currency}:{cat}", source_file),
            )
            n_cash += cur.rowcount

    conn.commit()
    print(f"[ibkr] {native_code}: loaded {n_positions} positions, {n_sec} security txns, {n_cash} cash txns")
    return {"n_positions": n_positions, "n_sec": n_sec, "n_cash": n_cash}


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 2 and sys.argv[2] == "--load":
        from db import get_connection
        conn = get_connection()
        force = "--force" in sys.argv
        load_ibkr_statement(sys.argv[1], conn, force=force)
        conn.close()
    else:
        r = parse_statement(sys.argv[1])
        print(json.dumps(r, indent=2, default=str))
