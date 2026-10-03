"""
Parses a DBS "Consolidated Statement" PDF. Real text layer (no OCR needed).

What's in this statement, and what we do with each part (per explicit scope
agreement - see git history / README for the full discussion):

  - DBS Multiplier Account / POSB eSavings - ordinary bank cash, multi-
    currency for Multiplier (SGD/AUD/USD). Fully parsed: positions (cash by
    currency) AND complete transaction history, reconciliation-gated the
    same way as every other cash ledger in this project.

  - CPF Investment Scheme (CPFIS-OA) and Supplementary Retirement Scheme
    (SRS) - POSITIONS ONLY, no transaction history this phase (deferred:
    the per-transaction fees DBS charges for moving money to a broker -
    TRANSACTION FEE, GST - aren't captured yet). Within positions, only a
    SUBSET is loaded, by explicit decision:
      - Cash balance: included (not yet moved anywhere else)
      - Direct equity/bond/unit-trust holdings: included (not captured by
        any other source - CDP statements don't show CPF/SRS-funded buys)
      - "FUND MGT - UOB KAY HIAN": EXCLUDED - this is the ORIGINAL amount
        placed with the broker also used by Endowus Single (confirmed:
        Endowus Single's own statement shows "UOBKH acct no. 2151636").
        Endowus Single's own position values are the more accurate,
        current figure for this same money - loading both would double-
        count the same underlying capital at two different points in time.
      - "FUND MGT - NAVIGATOR": EXCLUDED - this is Dollardex, explicitly
        out of scope (being migrated to Endowus; negligible balance left).
      - SRS's "UOB KAY HIAN" fund-management line: excluded, same reasoning.
      - SRS's "SINGAPORE LIFE LTD." insurance placement: INCLUDED - not
        captured anywhere else, cost-basis only (statement shows no
        separate live market value for it).
    The reconciliation check still PARSES every line in these sections
    (including the excluded ones) and validates against each subsection's
    own stated "Total:" line - this validates extraction correctness
    independent of the inclusion/exclusion business decision, which is
    applied only afterward, at load time.

  - Mortgage Loan: fully excluded, no liability tracking. The mortgage
    PAYMENT itself still appears as an ordinary cash outflow in the
    Multiplier Account's own transaction ledger (nothing special needed -
    it's just a withdrawal row there, same as any other payment).
"""
import re
from pathlib import Path

import pdfplumber

from db import is_source_file_loaded

MONEY = r"[\d,]+\.\d{2}"


def _f(s):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _iso_date(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if not m:
        return None
    d, mth, y = m.groups()
    return f"{y}-{mth}-{d}"


# ---------------------------------------------------------------------------
def parse_cash_accounts_summary(text):
    """Page 1-style summary: returns [{account_name, account_no, currency,
    native_balance, sgd_equiv}, ...] - one row per account+currency."""
    rows = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(rf"^(DBS Multiplier Account|POSB eSavings Account)\s+(\S+)\s+"
                     rf"([A-Z]{{3}})\s+({MONEY})\s+({MONEY})$", line)
        if m:
            name, acct_no, ccy, native, sgd_equiv = m.groups()
            rows.append({"account_name": name, "account_no": acct_no, "currency": ccy,
                         "native_balance": _f(native), "sgd_equiv": _f(sgd_equiv)})
            # possible continuation lines: "AUD 983.20 876.86" (same account, more currencies)
            j = i + 1
            while j < len(lines):
                m2 = re.match(rf"^([A-Z]{{3}})\s+({MONEY})\s+({MONEY})$", lines[j].strip())
                if not m2:
                    break
                ccy2, native2, sgd2 = m2.groups()
                rows.append({"account_name": name, "account_no": acct_no, "currency": ccy2,
                             "native_balance": _f(native2), "sgd_equiv": _f(sgd2)})
                j += 1
            i = j
            continue
        i += 1
    return rows


def parse_cpf_srs_summary(text):
    """Returns {'CPF': {'account_no', 'total', 'cash_balance'}, 'SRS': {...}}."""
    out = {}
    m = re.search(rf"CPF Investment Account Total:\s*SGD\s*({MONEY})", text)
    cpf_total = _f(m.group(1)) if m else None
    m = re.search(rf"CPFIS-OA\s+(\S+)\s+({MONEY})", text)
    if m:
        out["CPF"] = {"account_no": m.group(1), "total": cpf_total, "cash_balance": _f(m.group(2))}

    m = re.search(rf"Supplementary Retirement Scheme Account Total:\s*SGD\s*({MONEY})", text)
    srs_total = _f(m.group(1)) if m else None
    m = re.search(rf"SRS Account\s+(\S+)\s+({MONEY})", text)
    if m:
        out["SRS"] = {"account_no": m.group(1), "total": srs_total, "cash_balance": _f(m.group(2))}
    return out


def parse_cpf_srs_holdings(text):
    """Returns (rows, totals). rows: list of {scheme, category, name, qty,
    unit_cost, total_cost, market_value, include}. category: 'EQUITY' (has
    live market value) or 'PLACEMENT' (cost-basis only - fund mgt /
    insurance placements). include: whether this line is in scope per the
    agreed design (excludes UOB Kay Hian and Navigator placements).
    totals: {(scheme, category): {total_cost, market_value}} - each
    subsection's OWN stated 'Total:' line, captured so the loader can
    validate parsed rows actually sum to it (catches silent under-capture,
    the same category of bug found and fixed in the Endowus Single build)."""
    rows = []
    totals = {}
    scheme, category = None, None
    EXCLUDE_NAMES = ("FUND MGT - UOB KAY HIAN", "FUND MGT - NAVIGATOR", "UOB KAY HIAN PTE LTD")

    for raw in text.split("\n"):
        line = raw.strip()
        if line.strip() == "CPF Investment Scheme":
            scheme, category = "CPF", None
            continue
        if line.strip() == "Supplementary Retirement Scheme":
            scheme, category = "SRS", None
            continue
        if line.startswith("Loans"):
            scheme, category = None, None
            continue
        if not scheme:
            continue
        if "Equities/" in line or "Unit Trusts/" in line:
            category = "EQUITY"
            continue
        if "Insurance/" in line or "Fund Management" in line or line.strip() == "l Insurance":
            category = "PLACEMENT"
            continue
        if "Account Total:" in line:
            continue
        if line.startswith("Name "):
            continue
        if line.startswith("Total:") and category:
            m = re.match(rf"^Total:\s*({MONEY})(?:\s+({MONEY}))?$", line)
            if m:
                tc, mv = m.groups()
                key = (scheme, category)
                # a category can have MULTIPLE sub-sections, each with its
                # own "Total:" line (e.g. SRS's PLACEMENT category has both
                # a Fund Management total and a separate Insurance total) -
                # accumulate across them, don't overwrite
                totals.setdefault(key, {"total_cost": 0.0, "market_value": 0.0})
                totals[key]["total_cost"] += _f(tc) or 0.0
                totals[key]["market_value"] += _f(mv) or 0.0
            continue
        if not category:
            continue

        if category == "EQUITY":
            m = re.match(rf"^(.+?)\s+({MONEY})\s+([\d.]+)\s+({MONEY})\s+({MONEY})$", line)
            if m:
                name, qty, unit_cost, total_cost, mv = m.groups()
                rows.append({
                    "scheme": scheme, "category": category, "name": name.strip(),
                    "qty": _f(qty), "unit_cost": _f(unit_cost),
                    "total_cost": _f(total_cost), "market_value": _f(mv),
                    "include": not any(name.strip().upper().startswith(x) for x in EXCLUDE_NAMES),
                })
        elif category == "PLACEMENT":
            m = re.match(rf"^(.+?)\s+({MONEY})$", line)
            if m:
                name, total_cost = m.groups()
                rows.append({
                    "scheme": scheme, "category": category, "name": name.strip(),
                    "qty": None, "unit_cost": None,
                    "total_cost": _f(total_cost), "market_value": None,
                    "include": not any(name.strip().upper().startswith(x) for x in EXCLUDE_NAMES),
                })
    return rows, totals


def parse_cash_transactions(text):
    """Returns (txns, blocks). txns: [{account_name, account_no, currency,
    trade_date, description, amount (signed), balance}]. blocks:
    {(account_name, account_no, currency): {start, end, w_total, d_total}}
    for reconciliation. Explicitly scoped to the Multiplier/eSavings cash
    accounts only - the CPF/SRS transaction sections (out of scope this
    phase) also contain their own "Balance Brought Forward" lines, which
    would otherwise cross-contaminate whichever cash account/currency was
    last active before that section started."""
    txns = []
    blocks = {}
    account_name, account_no, currency = None, None, "SGD"
    prev_balance = None
    in_scope = False

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"^(DBS Multiplier Account|POSB eSavings Account)\s+Account No\.\s+(\S+)$", line)
        if m:
            account_name, account_no = m.groups()
            currency = "SGD"
            in_scope = True
            i += 1
            continue
        if re.match(r"^(CPF Investment Account|Supplementary Retirement Scheme Account)\b", line):
            # out of scope this phase - stop attributing lines to the cash
            # account, so its own "Balance Brought Forward" etc. can't
            # cross-contaminate whatever cash account was active before it
            in_scope = False
            prev_balance = None
            i += 1
            continue
        if not in_scope:
            i += 1
            continue
        m = re.match(r"^CURRENCY:\s*(SINGAPORE DOLLAR|AUSTRALIAN DOLLAR|UNITED STATES DOLLAR)$", line)
        if m:
            currency = {"SINGAPORE DOLLAR": "SGD", "AUSTRALIAN DOLLAR": "AUD",
                        "UNITED STATES DOLLAR": "USD"}[m.group(1)]
            i += 1
            continue
        m = re.match(rf"^Balance Brought Forward\s+(?:(?:SGD|AUD|USD)\s+)?({MONEY})$", line)
        if m:
            prev_balance = _f(m.group(1))
            key = (account_name, account_no, currency)
            blocks.setdefault(key, {})
            if "start" not in blocks[key]:
                # only the FIRST occurrence is the true starting balance -
                # a multi-page block repeats this line as a page-break
                # continuation marker, which must not overwrite it
                blocks[key]["start"] = prev_balance
            i += 1
            continue
        m = re.match(rf"^Total Balance Carried Forward(?: in [A-Z]+)?:\s*({MONEY})\s+({MONEY})\s+({MONEY})$", line)
        if m:
            w, d, end = m.groups()
            key = (account_name, account_no, currency)
            blocks.setdefault(key, {})
            blocks[key]["w_total"] = _f(w)
            blocks[key]["d_total"] = _f(d)
            blocks[key]["end"] = _f(end)
            i += 1
            continue
        m = re.match(rf"^(\d{{2}}/\d{{2}}/\d{{4}})\s+(.+?)\s+({MONEY})\s+({MONEY})$", line)
        if m and prev_balance is not None:
            date_s, desc, amt_s, bal_s = m.groups()
            amt, bal = _f(amt_s), _f(bal_s)
            sign = 1 if round(bal - prev_balance, 2) == round(amt, 2) else \
                   (-1 if round(prev_balance - bal, 2) == round(amt, 2) else None)
            if sign is not None:
                txns.append({
                    "account_name": account_name, "account_no": account_no, "currency": currency,
                    "trade_date": _iso_date(date_s), "description": desc.strip(),
                    "amount": amt * sign, "balance": bal,
                })
                prev_balance = bal
        i += 1
    return txns, blocks


def parse_statement(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    m = re.search(r"as at (\d{1,2} \w{3} \d{4})", text)
    as_of_date = None
    if m:
        MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                  "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        dm = re.match(r"(\d{1,2}) (\w{3}) (\d{4})", m.group(1))
        if dm and dm.group(2) in MONTHS:
            as_of_date = f"{dm.group(3)}-{MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"

    cash_txns, cash_blocks = parse_cash_transactions(text)
    cpf_srs_holdings, cpf_srs_totals = parse_cpf_srs_holdings(text)

    return {
        "source_file": Path(pdf_path).name,
        "as_of_date": as_of_date,
        "cash_accounts": parse_cash_accounts_summary(text),
        "cpf_srs_summary": parse_cpf_srs_summary(text),
        "cpf_srs_holdings": cpf_srs_holdings,
        "cpf_srs_totals": cpf_srs_totals,
        "cash_transactions": cash_txns,
        "cash_blocks": cash_blocks,
    }


# ---------------------------------------------------------------------------
def load_dbs_statement(pdf_path, conn, force=False):
    from collections import defaultdict
    from classify import classify

    source_file = Path(pdf_path).name
    if not force and is_source_file_loaded(conn, "position_snapshot", source_file):
        print(f"[dbs] SKIPPED: '{source_file}' already loaded (use --force to reload anyway)")
        return None

    parsed = parse_statement(pdf_path)
    as_of = parsed["as_of_date"]

    # --- Gate 1: cash blocks (start + deposits - withdrawals = end) ---
    for key, block in parsed["cash_blocks"].items():
        if not all(k in block for k in ("start", "end", "w_total", "d_total")):
            print(f"[dbs] ABORT: incomplete cash block for {key} - {block}")
            return None
        computed_end = round(block["start"] + block["d_total"] - block["w_total"], 2)
        if abs(computed_end - block["end"]) > 0.02:
            print(f"[dbs] ABORT: cash block {key} does not reconcile "
                  f"(computed={computed_end}, stated={block['end']})")
            return None
    print(f"[dbs] Reconciliation OK: {len(parsed['cash_blocks'])} cash block(s) reconcile exactly")

    # --- Gate 2: CPF/SRS holdings sum to each subsection's own stated total.
    # Parses and validates EVERY line (including excluded ones) - this
    # confirms extraction correctness independent of the inclusion/exclusion
    # business decision, which is applied only afterward, at load time. ---
    computed_totals = defaultdict(lambda: {"total_cost": 0.0, "market_value": 0.0})
    for h in parsed["cpf_srs_holdings"]:
        key = (h["scheme"], h["category"])
        computed_totals[key]["total_cost"] += h["total_cost"] or 0.0
        computed_totals[key]["market_value"] += h["market_value"] or 0.0
    for key, stated in parsed["cpf_srs_totals"].items():
        computed = computed_totals.get(key, {"total_cost": 0.0, "market_value": 0.0})
        if (abs(computed["total_cost"] - stated["total_cost"]) > 0.02 or
                abs(computed["market_value"] - (stated["market_value"] or 0.0)) > 0.02):
            print(f"[dbs] ABORT: {key} holdings do not sum to the subsection's stated total "
                  f"(computed={computed}, stated={stated}). Not loading - check OCR/parsing output.")
            return None
    if parsed["cpf_srs_totals"]:
        print(f"[dbs] Reconciliation OK: all {len(parsed['cpf_srs_totals'])} CPF/SRS "
              f"subsection totals match")

    def resolve(native_code):
        row = conn.execute(
            "SELECT account_id FROM account WHERE institution_id='DBS' AND native_account_code=?",
            (native_code,),
        ).fetchone()
        return row["account_id"] if row else None

    n_positions, n_txns = 0, 0

    # --- Cash accounts: positions (one row per currency) ---
    for ca in parsed["cash_accounts"]:
        account_id = resolve(ca["account_no"])
        if not account_id:
            print(f"[dbs] ABORT: no account configured for '{ca['account_no']}' "
                  f"({ca['account_name']}) - add it to config/accounts.csv first.")
            return None
        conn.execute(
            "DELETE FROM position_snapshot WHERE account_id=? AND as_of_date=? AND position_currency=?",
            (account_id, as_of, ca["currency"]),
        )
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, market_value_base, funding_source, source_file)
               VALUES (?, NULL, 1, ?, ?, ?, ?, 'SGD Cash', ?)""",
            (account_id, as_of, ca["currency"], ca["native_balance"], ca["sgd_equiv"], source_file),
        )
        n_positions += 1

    # --- Cash transactions ---
    # A forced reload REPLACES this statement's transactions rather than adding to them (same
    # pattern as positions). INSERT OR IGNORE cannot do this job: cash rows have no instrument or
    # quantity, SQLite treats NULLs as distinct in a UNIQUE constraint, so it never fires and a
    # forced reload used to store every row a second time.
    conn.execute("DELETE FROM txn WHERE source_file=?", (source_file,))
    for i, t in enumerate(parsed["cash_transactions"]):
        account_id = resolve(t["account_no"])
        if not account_id:
            continue  # already validated above when loading positions
        txn_type, txn_subtype = classify(conn, t["description"])
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, gross_amount, raw_description, raw_txn_type_label,
                  source_reference, source_file)
               VALUES (?, NULL, 'CASH', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, t["trade_date"], txn_type, txn_subtype, t["currency"], t["amount"],
             t["description"], t["description"], f"{source_file}:CASH:{i}", source_file),
        )
        n_txns += cur.rowcount

    # --- CPF/SRS: positions only this phase (cash balance + included holdings) ---
    native_code_map = {"CPF": None, "SRS": None}
    summary = parsed["cpf_srs_summary"]
    for scheme, info in summary.items():
        account_id = resolve(info["account_no"])
        if not account_id:
            print(f"[dbs] ABORT: no account configured for {scheme} account "
                  f"'{info['account_no']}' - add it to config/accounts.csv first.")
            return None
        native_code_map[scheme] = info["account_no"]
        funding_tag = "CPF OA" if scheme == "CPF" else "SRS"
        conn.execute(
            "DELETE FROM position_snapshot WHERE account_id=? AND as_of_date=?",
            (account_id, as_of),
        )
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, market_value_base, funding_source, source_file)
               VALUES (?, NULL, 1, ?, 'SGD', ?, ?, ?, ?)""",
            (account_id, as_of, info["cash_balance"], info["cash_balance"], funding_tag, source_file),
        )
        n_positions += 1

    for h in parsed["cpf_srs_holdings"]:
        if not h["include"]:
            continue
        native_code = native_code_map.get(h["scheme"])
        account_id = resolve(native_code) if native_code else None
        if not account_id:
            continue
        funding_tag = "CPF OA" if h["scheme"] == "CPF" else "SRS"
        instrument_id = f"DBS:{h['name']}"
        conn.execute(
            """INSERT INTO instrument (instrument_id, name, asset_class, instrument_currency)
               VALUES (?, ?, ?, 'SGD')
               ON CONFLICT(instrument_id) DO UPDATE SET
                 asset_class=COALESCE(instrument.asset_class, excluded.asset_class)""",
            (instrument_id, h["name"], "Equities" if h["category"] == "EQUITY" else "Insurance/Fixed Deposits"),
        )
        # market_value may be absent (cost-basis-only placements, e.g.
        # Singapore Life) - fall back to total_cost, same pattern as CDP's
        # Savings Bonds (no live market price available)
        mv = h["market_value"] if h["market_value"] is not None else h["total_cost"]
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, cost_price, cost_value_native, market_value_base,
                  funding_source, source_file)
               VALUES (?, ?, 0, ?, 'SGD', ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, as_of, h["qty"], h["unit_cost"], h["total_cost"],
             mv, funding_tag, source_file),
        )
        n_positions += 1

    conn.commit()
    print(f"[dbs] {source_file}: loaded {n_positions} positions, {n_txns} cash txns")
    return {"n_positions": n_positions, "n_txns": n_txns}


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 2 and sys.argv[2] == "--load":
        from db import get_connection
        conn = get_connection()
        force = "--force" in sys.argv
        load_dbs_statement(sys.argv[1], conn, force=force)
        conn.close()
    else:
        r = parse_statement(sys.argv[1])
        r["cash_blocks"] = {str(k): v for k, v in r["cash_blocks"].items()}
        r["cpf_srs_totals"] = {str(k): v for k, v in r["cpf_srs_totals"].items()}
        print(json.dumps(r, indent=2, default=str))
