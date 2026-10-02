"""
Parses an Endowus monthly statement PDF (scanned, no text layer - requires
OCR) into positions and transactions, mapped onto the same schema as the
UBS ingestion.

Endowus structure, for context:
  - One "Goal" = one labeled sub-portfolio, almost always holding a single
    fund. Goals are NOT separate custody accounts - the whole relationship
    (all goals combined) is modelled here as ONE account.
  - "Cash Balance" section (pages near the end) = the CASH leg: starting/
    ending uninvested cash balance plus every fee, deposit, buy, sell and
    distribution as its own line. This is the fee/income/deposit source
    of truth, the same role UBS's per-currency cash ledgers played.
  - Each Goal's own "Transactions" page = the SECURITY leg: unit-level
    trade detail (units, price) for that goal's fund.
  - The "All Investment Goals" overview page gives a per-goal Starting +
    Investments - Redemptions + Gain/loss = Ending identity - used here
    purely as a reconciliation check, not as a data source, the same way
    UBS's running cash balance validated (rather than fed) our parsing.

Because this PDF has no text layer, every number here comes from OCR
(tesseract at 300 DPI), which is meaningfully less trustworthy than a
native text extraction. DO NOT relax the reconciliation checks in
load_endowus_statement() - they're what make this safe to rely on despite
the OCR risk. A statement that fails reconciliation should be treated as
unparsed, not silently loaded.
"""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract

from db import is_source_file_loaded

# On Windows, the Tesseract OCR engine (a separate install from the
# pytesseract Python wrapper) often isn't on PATH even after installing.
# Fall back to the default install location rather than requiring the user
# to get PATH exactly right.
if os.name == "nt" and not shutil.which("tesseract"):
    _default_win_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_default_win_path):
        pytesseract.pytesseract.tesseract_cmd = _default_win_path

MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

# Cash-leg transaction-type keywords, longest-prefix-first, with the sign
# convention validated against the statement's own starting->ending balance
# arithmetic (see module docstring / build notes).
CASH_TXN_TYPES = [
    ("Distribution reinvested Buy", "BUY", "REINVESTMENT", -1),
    ("Distribution received", "INCOME", "DISTRIBUTION", +1),
    ("Investment Buy", "BUY", "FUND_PURCHASE", -1),
    ("Redemption Sell", "SELL", "FUND_REDEMPTION", +1),
    ("Endowus Fee", "FEE", "ADVISORY_FEE", -1),
    ("Deposit", "DEPOSIT", "EXTERNAL_INCOMING", +1),
    ("Withdrawal", "WITHDRAWAL", "EXTERNAL_OUTGOING", -1),
]


def _to_float(s):
    if s is None:
        return None
    s = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_iso_date(s):
    m = re.match(r"(\d{1,2}) (\w{3})\w* (\d{4})", s)
    if not m:
        return None
    day, mon, year = m.groups()
    mon3 = mon[:3]
    if mon3 not in MONTHS:
        return None
    return f"{year}-{MONTHS[mon3]:02d}-{int(day):02d}"


def ocr_pages(pdf_path, dpi=300):
    pdf = pdfium.PdfDocument(pdf_path)
    pages = []
    for i in range(len(pdf)):
        bitmap = pdf[i].render(scale=dpi / 72)
        img = bitmap.to_pil()
        pages.append(pytesseract.image_to_string(img))
    return pages


def classify_page(text):
    if "All Investment Goals" in text and "Starting balance" in text:
        return "OVERVIEW"
    if "Cash Balance" in text and ("Cash Starting Balance" in text or "Cash Transactions" in text):
        return "CASH_BALANCE"
    if "Aggregated Asset Allocation" in text:
        return "AGG_ALLOCATION"
    if "Asset Allocation" in text and "Transactions" not in text:
        return "GOAL_ALLOCATION"
    if "Transactions" in text and "Trade date" in text:
        return "GOAL_TRANSACTIONS"
    return "OTHER"


MONEY = r"S?\${1,2}([\d,]+\.?\d*)"
# Known funding sources. SGD Cash was the only one ever seen in the Joint
# account's statement; CPF OA/SA and SRS show up in the Single account.
# An explicit alternation (not a generic wildcard) is deliberate - fund
# names can contain arbitrary words, so a vague pattern risks over- or
# under-matching into adjacent text.
FUNDING_SOURCE = r"(SGD Cash|CPF OA|CPF SA|SRS)"
ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")


def _fix_isin(s):
    """OCR commonly confuses O<->0 in ISINs. ISINs are 2 letters + 10
    alphanumerics; the last 10 chars are conventionally digits-heavy but can
    contain letters, so we only fix the pattern where it's unambiguous:
    leave as-is if it already matches the ISIN shape."""
    return s


def parse_goal_name(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for l in lines[:4]:
        if l.startswith("Reee") or l.startswith("Neen") or l.startswith("in.") or "Page" in l:
            continue
        if len(l) <= 4 and l.islower():
            # stray OCR noise on its own line before the real header -
            # seen as both "a" and "eee". Real goal names in this document
            # are always title-case or multi-word, never short all-lowercase
            # strings, so this heuristic is safe rather than a fixed length cap.
            continue
        if re.search(r"(.)\1{2,}", l):
            # a different, LONGER OCR noise artifact - e.g.
            # "Neeeeeeeeeeeeeee reer reer r rer er rr rere errr ree reer eee"
            # (found on a user's own machine - different Tesseract build/
            # version than this project's own testing, confirming OCR noise
            # patterns aren't fully predictable across environments for the
            # identical source PDF). 3+ identical characters in a row is
            # essentially never real text, regardless of overall length, so
            # this generalizes better than a length-based filter alone.
            continue
        cleaned = re.sub(r"\s*Statement Period\s*$", "", l).strip()
        if cleaned and "Plan Currency" not in cleaned and "Display Currency" not in cleaned:
            return cleaned
    return None


# ---------------------------------------------------------------------------
_OVERVIEW_BOILERPLATE = (
    "Goal name", "Starting balance", "(01 ", "(31 ", "(Buy)", "(Sell)",
    "Statement Period", "Display Currency", "All Investment Goals",
    "(Continued)", "in.sgsr", "Page ", "Investment Starting Balance",
    "Investment Ending Balance", "Returns (",
)


def parse_overview_pages(pages_text):
    """Returns {goal_name: {starting, investments, redemptions, gain_loss, ending, funding_source}}
    - used only to validate the goal-allocation totals, not loaded directly."""
    out = {}
    current_goal = None
    row_re = re.compile(
        rf"^{FUNDING_SOURCE}\s+{MONEY}\s+{MONEY}\s+{MONEY}\s+(-?)\s*{MONEY}\s+{MONEY}$")
    for text in pages_text:
        if classify_page(text) != "OVERVIEW":
            continue
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            m = row_re.match(line)
            if m and current_goal:
                sign = -1 if m.group(5) == "-" else 1
                out[current_goal] = {
                    "funding_source": m.group(1),
                    "starting": _to_float(m.group(2)), "investments": _to_float(m.group(3)),
                    "redemptions": _to_float(m.group(4)),
                    "gain_loss": _to_float(m.group(6)) * sign,
                    "ending": _to_float(m.group(7)),
                }
            elif "$" not in line and not any(bp in line for bp in _OVERVIEW_BOILERPLATE):
                # any non-data, non-boilerplate line is a goal-name header -
                # more robust than requiring strict ALL-CAPS, since OCR can
                # flip an 'I' to 'l' and break an isupper() check
                current_goal = line.title()
    return out
    return out


def _try_fallback_split_row(lines, goal_name, page_type):
    """Single-holding (100% allocation), newly-opened goal pages can split
    the allocation row across the page: fund name/class/funding/units/price
    land on one line, but 'Latest value'/'Allocation %' end up separated
    onto a different line entirely (confirmed via the Single account's
    Amundi and Schroder goals - both newly opened this period). Tries the
    leading fields alone, then searches the rest of the page for a
    standalone '$value pct%' line to supply what's missing."""
    lead_re = re.compile(
        r"^(.+?)\s+(Equity Fund|Multi Asset|Bond Fund|Money Market|Fixed|Alternative)\s+" +
        FUNDING_SOURCE + rf"\s+([\d,]+\.?\d*)\s+{MONEY}\s+{MONEY}$")
    trailing_re = re.compile(rf"^{MONEY}\s+([\d.]+)%$")

    lead_match, lead_idx = None, None
    for i, raw in enumerate(lines):
        m = lead_re.match(raw.strip())
        if m:
            lead_match, lead_idx = m, i
            break
    if not lead_match:
        return None

    fund_name = lead_match.group(1).strip()
    isin = None
    if lead_idx + 1 < len(lines):
        isin_m = ISIN_RE.search(lines[lead_idx + 1])
        if isin_m:
            isin = isin_m.group(1)

    value, pct = None, None
    for raw in lines[lead_idx + 1:]:
        m = trailing_re.match(raw.strip())
        if m:
            value, pct = _to_float(m.group(1)), _to_float(m.group(2))
            break
    if value is None:
        return None

    return {
        "goal_name": goal_name or fund_name, "fund_name": fund_name, "isin": isin,
        "asset_class": "Fixed Income" if lead_match.group(2) == "Fixed" else lead_match.group(2),
        "funding_source": lead_match.group(3),
        "units": _to_float(lead_match.group(4)),
        "nav": _to_float(lead_match.group(5)), "avg_price": _to_float(lead_match.group(6)),
        "value": value, "allocation_pct": pct,
        "source": page_type, "parsed_via": "fallback_split_row",
    }


def parse_allocation_pages(pages_text):
    """Returns list of {goal_name, fund_name, isin, asset_class, units, nav,
    avg_price, value, allocation_pct}."""
    rows = []
    for text in pages_text:
        page_type = classify_page(text)
        if page_type not in ("GOAL_ALLOCATION", "AGG_ALLOCATION"):
            continue
        goal_name = parse_goal_name(text) if page_type == "GOAL_ALLOCATION" else None
        lines = [l for l in text.split("\n")]
        rows_before = len(rows)
        pending_isin = None
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(
                r"^(.+?)\s+(Equity Fund|Multi Asset|Bond Fund|Money Market|Fixed|Alternative)\s+" +
                FUNDING_SOURCE + r"\s+"
                rf"([\d,]+\.?\d*)\s+{MONEY}\s+{MONEY}\s+{MONEY}\s+([\d.]+)%$",
                line)
            if m:
                fund_name = m.group(1).strip()
                # ISIN may be on the next line (fund name wrapped to 2 lines)
                isin = None
                if i + 1 < len(lines):
                    isin_m = ISIN_RE.search(lines[i + 1])
                    if isin_m:
                        isin = isin_m.group(1)
                    elif i + 2 < len(lines):
                        isin_m2 = ISIN_RE.search(lines[i + 2])
                        if isin_m2:
                            isin = isin_m2.group(1)
                            fund_name = f"{fund_name} {lines[i+1].strip()}"
                rows.append({
                    "goal_name": goal_name or fund_name, "fund_name": fund_name, "isin": isin,
                    "asset_class": "Fixed Income" if m.group(2) == "Fixed" else m.group(2),
                    "funding_source": m.group(3),
                    "units": _to_float(m.group(4)),
                    "nav": _to_float(m.group(5)), "avg_price": _to_float(m.group(6)),
                    "value": _to_float(m.group(7)), "allocation_pct": _to_float(m.group(8)),
                    "source": page_type,
                })
            i += 1
        if page_type == "GOAL_ALLOCATION" and len(rows) == rows_before:
            fallback = _try_fallback_split_row(lines, goal_name, page_type)
            if fallback:
                rows.append(fallback)
    return rows


def parse_goal_transactions_pages(pages_text):
    """Returns list of {goal_name, trade_date, txn_type_raw, details, funding_source, units, price, amount}."""
    rows = []
    for text in pages_text:
        if classify_page(text) != "GOAL_TRANSACTIONS":
            continue
        goal_name = parse_goal_name(text)
        for line in text.split("\n"):
            line = line.strip()
            m = re.match(
                r"^(\d{1,2} \w{3}\w* \d{4})\s+(Investment|Redemption|Distribution reinvested|Distribution received|Fee)\s+"
                rf"(Buy|Sell|In Endowus cash balance for)?\s*(.+?)\s+" + FUNDING_SOURCE +
                rf"\s+([\d,]+\.?\d*)\s+{MONEY}\s+{MONEY}$",
                line)
            if m:
                rows.append({
                    "goal_name": goal_name,
                    "trade_date": _to_iso_date(m.group(1)),
                    "txn_type_raw": m.group(2), "side": m.group(3),
                    "details": m.group(4).strip(), "funding_source": m.group(5),
                    "units": _to_float(m.group(6)), "price": _to_float(m.group(7)),
                    "amount": _to_float(m.group(8)),
                })
    return rows


def parse_cash_balance(pages_text):
    """Returns {starting_balance, ending_balance, start_date, end_date, transactions:[...]}."""
    result = {"starting_balance": None, "ending_balance": None,
              "start_date": None, "end_date": None, "transactions": []}
    for text in pages_text:
        if classify_page(text) != "CASH_BALANCE":
            continue
        m = re.search(r"Cash Starting Balance \((\d{1,2} \w{3}\w* \d{4})\)\s*\n?S\$\s*([\d,]+\.\d{2})", text)
        if m:
            result["start_date"] = _to_iso_date(m.group(1))
            result["starting_balance"] = _to_float(m.group(2))
        m = re.search(r"Cash Ending Balance \((\d{1,2} \w{3}\w* \d{4})\)\s*\n?S\$\s*([\d,]+\.\d{2})", text)
        if m:
            result["end_date"] = _to_iso_date(m.group(1))
            result["ending_balance"] = _to_float(m.group(2))

        for line in text.split("\n"):
            line = line.strip()
            date_m = re.match(r"^(\d{1,2} \w{3}\w* \d{4})\s+(.*)$", line)
            if not date_m:
                continue
            trade_date = _to_iso_date(date_m.group(1))
            rest = date_m.group(2)
            m = re.search(rf"{FUNDING_SOURCE}\s+{MONEY}\s*$", rest)
            if not trade_date or not m:
                continue
            amount = _to_float(m.group(2))
            desc = rest[:m.start()].strip()
            matched_type = None
            for prefix, txn_type, subtype, sign in CASH_TXN_TYPES:
                if desc.startswith(prefix):
                    matched_type = (txn_type, subtype, sign)
                    details = desc[len(prefix):].strip()
                    if details.startswith("for "):
                        details = details[4:]
                    if details.startswith("From "):
                        details = details[5:]
                    break
            if not matched_type:
                continue
            txn_type, subtype, sign = matched_type
            result["transactions"].append({
                "trade_date": trade_date, "raw_label": desc.split(" ")[0] + (
                    " " + desc.split(" ")[1] if len(desc.split(" ")) > 1 else ""),
                "txn_type": txn_type, "txn_subtype": subtype, "funding_source": m.group(1),
                "raw_description": details, "gross_amount": amount * sign,
            })
    return result


# ---------------------------------------------------------------------------
# Explicit account identification by email - this document's footer
# repeats the account holder's email on every single page (e.g.
# "in.sgsr@gmail.com Page 25 out of 66"), which is a reliable, repeating
# signal to auto-detect which Endowus account a statement belongs to,
# unlike CDP/DBS/IBKR, none of which print anything this directly
# identifying. Explicit mapping only, same principle as every other
# config-driven lookup in this project - an unrecognized email aborts
# rather than silently guessing (see load_endowus_statement()).
EMAIL_TO_ACCOUNT = {
    "in.sgsr@gmail.com": "Endowus Joint",
    "subsun.sg@gmail.com": "Endowus Single",
}


def _extract_account_email(pages_text):
    # OCR sometimes introduces spacing artifacts around '@' and '.' (seen
    # on the Single account's footer: "subsun.sg @ gmail.com" instead of
    # a clean string) - tolerate optional whitespace there, then strip it
    # from the result before using it for lookup.
    email_re = re.compile(r"[\w.+-]+\s*@\s*[\w-]+\s*\.\s*\w+")
    for text in pages_text:
        m = email_re.search(text)
        if m:
            return re.sub(r"\s+", "", m.group(0))
    return None


def parse_statement(pdf_path):
    pages_text = ocr_pages(pdf_path)

    # The "All Investment Goals" overview page carries the period and
    # headline figures - don't assume it's pages_text[0], since some
    # statements (the Single account, confirmed) have a cover page before
    # it that the Joint account's statements didn't have.
    overview_page = next((t for t in pages_text if classify_page(t) == "OVERVIEW"), pages_text[0])

    m = re.search(r"(\d{2}) (\w{3})\w* - (\d{2}) (\w{3})\w* (\d{4})", overview_page)
    period_end = None
    if m:
        d2, mon2, year = m.group(3), m.group(4), m.group(5)
        mon3 = mon2[:3]
        if mon3 in MONTHS:
            period_end = f"{year}-{MONTHS[mon3]:02d}-{int(d2):02d}"
    if period_end is None:
        # alternate format seen on the Single account's cover page:
        # "01 Jul 2026 to 31 Jul 2026" (each date has its own year)
        m2 = re.search(r"\d{1,2} \w{3}\w* \d{4} to (\d{1,2}) (\w{3})\w* (\d{4})",
                        pages_text[0] + "\n" + overview_page)
        if m2:
            d2, mon2, year = m2.group(1), m2.group(2), m2.group(3)
            mon3 = mon2[:3]
            if mon3 in MONTHS:
                period_end = f"{year}-{MONTHS[mon3]:02d}-{int(d2):02d}"

    headline = {}
    m = re.search(
        rf"Investment Starting Balance \((\d{{2}} \w{{3}}\w* \d{{4}})\)\s*"
        rf"Investment Ending Balance \((\d{{2}} \w{{3}}\w* \d{{4}})\)\s*"
        rf"Returns \([^)]+\)\s*\n\s*"
        rf"{MONEY}\s+{MONEY}\s+(-?)\s*{MONEY}",
        pages_text[0])
    if not m:
        m = re.search(
            rf"Investment Starting Balance \((\d{{2}} \w{{3}}\w* \d{{4}})\)\s*"
            rf"Investment Ending Balance \((\d{{2}} \w{{3}}\w* \d{{4}})\)\s*"
            rf"Returns \([^)]+\)\s*\n\s*"
            rf"{MONEY}\s+{MONEY}\s+(-?)\s*{MONEY}",
            overview_page)
    if m:
        headline["start_date"] = _to_iso_date(m.group(1))
        headline["starting_balance"] = _to_float(m.group(3))
        headline["end_date"] = _to_iso_date(m.group(2))
        headline["ending_balance"] = _to_float(m.group(4))
        sign = -1 if m.group(5) == "-" else 1
        headline["returns"] = _to_float(m.group(6)) * sign

    return {
        "period_end": period_end,
        "overview": parse_overview_pages(pages_text),
        "allocations": parse_allocation_pages(pages_text),
        "goal_transactions": parse_goal_transactions_pages(pages_text),
        "cash_balance": parse_cash_balance(pages_text),
        "headline": headline,
        "source_file": Path(pdf_path).name,
        "account_email": _extract_account_email(pages_text),
    }


def modified_dietz(bmv, emv, flows, period_start, period_end):
    """flows: list of (date_iso, amount). Positive = capital entering the
    invested pool (a Buy), negative = leaving (a Sell/Redemption)."""
    total_days = (datetime.fromisoformat(period_end) - datetime.fromisoformat(period_start)).days
    if total_days <= 0:
        return None
    net_cf = sum(amt for _, amt in flows)
    weighted_cf = 0.0
    for dt, amt in flows:
        days_in = (datetime.fromisoformat(dt) - datetime.fromisoformat(period_start)).days
        w = (total_days - days_in) / total_days
        weighted_cf += amt * w
    denom = bmv + weighted_cf
    if denom == 0:
        return None
    return (emv - bmv - net_cf) / denom


# ---------------------------------------------------------------------------
def load_endowus_statement(pdf_path, conn, account_native_code=None, force=False):
    source_file = Path(pdf_path).name
    if not force and is_source_file_loaded(conn, "position_snapshot", source_file):
        print(f"[endowus] SKIPPED: '{source_file}' already loaded (use --force to reload anyway) "
              f"- OCR not re-run.")
        return None

    parsed = parse_statement(pdf_path)
    source_file = parsed["source_file"]

    # Resolve which Endowus account this is from the statement's own
    # footer email, unless the caller explicitly overrode it - closes the
    # risk the old hardcoded default ("Endowus Joint") carried: forgetting
    # to pass account_native_code for a Single-account statement used to
    # silently load it under Joint instead, with no error at all.
    if account_native_code is None:
        email = parsed["account_email"]
        if not email or email not in EMAIL_TO_ACCOUNT:
            print(f"[endowus] ABORT: could not identify which Endowus account {source_file} "
                  f"belongs to (footer email: {email!r}, not in the known mapping) - and no "
                  f"account_native_code override was given. Not loading - if this is a genuinely "
                  f"new Endowus account, add its email to EMAIL_TO_ACCOUNT first.")
            return None
        account_native_code = EMAIL_TO_ACCOUNT[email]
        print(f"[endowus] Auto-detected account from footer email ({email}): {account_native_code}")

    # --- Gate on reconciliation before touching the database. OCR is far
    # less trustworthy than the UBS text-layer extracts, so a statement
    # that doesn't reconcile cleanly should not be loaded at all. ---
    cb = parsed["cash_balance"]
    if cb["starting_balance"] is None or cb["ending_balance"] is None:
        print(f"[endowus] ABORT: could not find cash starting/ending balance in {source_file}")
        return None
    replayed = cb["starting_balance"] + sum(t["gross_amount"] for t in cb["transactions"])
    diff = round(replayed - cb["ending_balance"], 2)
    if abs(diff) > 0.02:
        print(f"[endowus] ABORT: cash balance does not reconcile for {source_file} "
              f"(replayed={replayed:.2f}, stated ending={cb['ending_balance']:.2f}, diff={diff:.2f}). "
              f"Not loading - check OCR output before retrying.")
        return None
    print(f"[endowus] Reconciliation OK: {cb['starting_balance']:.2f} + flows = "
          f"{replayed:.2f} (matches stated ending {cb['ending_balance']:.2f})")

    # --- Second, independent gate: per-goal identity (Starting + Buy - Sell
    # + Gain/loss = Ending). This is the PRIMARY check for any goal funded
    # by CPF/SRS, since those bypass the cash pool entirely - the cash-
    # balance check above would pass trivially (0=0) for such an account
    # without validating a single dollar of real activity. Verified to hold
    # exactly for both the Joint and Single accounts, so this runs
    # universally rather than only when CPF/SRS funding is detected. ---
    overview = parsed["overview"]
    if not overview:
        print(f"[endowus] ABORT: could not parse the per-goal overview table in {source_file} "
              f"- nothing to validate against.")
        return None
    goal_check_failed = False
    for goal_name, g in overview.items():
        expected_ending = g["starting"] + g["investments"] - g["redemptions"] + g["gain_loss"]
        goal_diff = round(expected_ending - g["ending"], 2)
        if abs(goal_diff) > 0.02:
            print(f"[endowus] ABORT: goal '{goal_name}' does not reconcile "
                  f"(starting {g['starting']:.2f} + investments {g['investments']:.2f} "
                  f"- redemptions {g['redemptions']:.2f} + gain/loss {g['gain_loss']:.2f} "
                  f"= {expected_ending:.2f}, but stated ending is {g['ending']:.2f}, "
                  f"diff={goal_diff:.2f}). Not loading - check OCR output before retrying.")
            goal_check_failed = True
    if goal_check_failed:
        return None
    print(f"[endowus] Reconciliation OK: all {len(overview)} goals reconcile exactly "
          f"(starting + investments - redemptions + gain/loss = ending, per goal)")

    # --- Third, independent gate: cross-check that POSITIONS (loaded from
    # the allocation table) actually agree with the overview table's
    # validated per-goal ending balance. The first two gates both validate
    # the overview table's own internal arithmetic - neither one confirms
    # that the allocation table (what positions actually get loaded FROM)
    # captured every goal completely. This gap is exactly what let two
    # whole goals silently vanish from a prior Single-account load (an OCR
    # layout variant on newly-opened, single-holding goal pages split the
    # row across lines in a way the parser didn't handle) while both other
    # gates still reported success, since they never touched the
    # allocation table at all. ---
    alloc_totals_by_goal = {}
    for row in parsed["allocations"]:
        if row["source"] != "GOAL_ALLOCATION" or not row["value"]:
            continue
        key = (row["goal_name"] or row["fund_name"]).strip().lower()
        alloc_totals_by_goal[key] = alloc_totals_by_goal.get(key, 0.0) + row["value"]

    alloc_check_failed = False
    for goal_name, g in overview.items():
        key = goal_name.strip().lower()
        alloc_total = alloc_totals_by_goal.get(key)
        if alloc_total is None:
            if abs(g["ending"]) < 0.02:
                # genuinely zero (e.g. fully redeemed this period) - no
                # allocation row is correct here, not a parsing failure
                continue
            print(f"[endowus] ABORT: goal '{goal_name}' has an overview entry (ending "
                  f"{g['ending']:.2f}) but NO allocation-table rows were captured for it at "
                  f"all - this goal would silently vanish from positions. Not loading - "
                  f"check OCR output before retrying.")
            alloc_check_failed = True
            continue
        alloc_diff = round(alloc_total - g["ending"], 2)
        if abs(alloc_diff) > 0.02:
            print(f"[endowus] ABORT: goal '{goal_name}' allocation-table total "
                  f"({alloc_total:.2f}) does not match its overview-table ending "
                  f"({g['ending']:.2f}), diff={alloc_diff:.2f}. Not loading - check OCR "
                  f"output before retrying.")
            alloc_check_failed = True
    if alloc_check_failed:
        return None
    print(f"[endowus] Reconciliation OK: allocation-table totals match the overview table's "
          f"ending balance for all {len(overview)} goals - positions are complete.")

    account_id = conn.execute(
        "SELECT account_id FROM account WHERE institution_id='Endowus' AND native_account_code=?",
        (account_native_code,),
    ).fetchone()
    if not account_id:
        print(f"[endowus] ABORT: no account configured for '{account_native_code}' - "
              f"add it to config/accounts.csv first.")
        return None
    account_id = account_id["account_id"]

    conn.execute(
        """INSERT INTO reconciliation_log
             (account_id, scope, period_start, period_end, expected_value, actual_value, difference, status, notes)
           VALUES (?, 'ENDOWUS_CASH_BALANCE_REPLAY', ?, ?, ?, ?, ?, 'OK', ?)""",
        (account_id, cb["start_date"], cb["end_date"], replayed, cb["ending_balance"], diff,
         f"Starting {cb['starting_balance']:.2f} + {len(cb['transactions'])} cash flows "
         f"reconciles to stated ending balance (source: {source_file})"),
    )
    conn.execute(
        """INSERT INTO reconciliation_log
             (account_id, scope, period_start, period_end, status, notes)
           VALUES (?, 'ENDOWUS_PER_GOAL_IDENTITY', ?, ?, 'OK', ?)""",
        (account_id, cb.get("start_date"), parsed["period_end"],
         f"All {len(overview)} goals reconcile exactly: starting + investments - redemptions "
         f"+ gain/loss = ending (source: {source_file})"),
    )
    conn.execute(
        """INSERT INTO reconciliation_log
             (account_id, scope, period_start, period_end, status, notes)
           VALUES (?, 'ENDOWUS_ALLOCATION_VS_OVERVIEW', ?, ?, 'OK', ?)""",
        (account_id, cb.get("start_date"), parsed["period_end"],
         f"Allocation-table totals match the overview table's ending balance for all "
         f"{len(overview)} goals - positions are complete (source: {source_file})"),
    )
    conn.commit()

    # --- Positions: one row per fund holding (from GOAL_ALLOCATION pages,
    # the per-goal detail - not AGG_ALLOCATION, which repeats the same
    # holdings in a combined table and would double-count if also loaded) ---
    n_positions = 0
    conn.execute(
        "DELETE FROM position_snapshot WHERE account_id=? AND as_of_date=?",
        (account_id, parsed["period_end"]),
    )
    for row in parsed["allocations"]:
        if row["source"] != "GOAL_ALLOCATION" or not row["value"]:
            continue
        instrument_id = row["isin"] or f"NAME:{row['fund_name']}"
        conn.execute(
            """INSERT INTO instrument (instrument_id, isin, name, asset_class, instrument_currency)
               VALUES (?, ?, ?, ?, 'SGD')
               ON CONFLICT(instrument_id) DO UPDATE SET
                 name=excluded.name,
                 asset_class=COALESCE(instrument.asset_class, excluded.asset_class)""",
            (instrument_id, row["isin"], row["fund_name"], row["asset_class"]),
        )
        conn.execute(
            """INSERT INTO position_snapshot
                 (account_id, instrument_id, is_cash, as_of_date, position_currency,
                  quantity, cost_price, market_price, market_value_base,
                  pct_of_portfolio, funding_source, source_file)
               VALUES (?, ?, 0, ?, 'SGD', ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, parsed["period_end"], row["units"],
             row["avg_price"], row["nav"], row["value"], row["allocation_pct"],
             row.get("funding_source"), source_file),
        )
        n_positions += 1
    conn.commit()  # commit positions/instruments before the txn loops reference them

    # cash position row - this is specifically the SGD cash pool (confirmed:
    # CPF/SRS-funded goals bypass it entirely, so it's never a mix)
    conn.execute(
        """INSERT INTO position_snapshot
             (account_id, instrument_id, is_cash, as_of_date, position_currency,
              quantity, market_value_base, funding_source, source_file)
           VALUES (?, NULL, 1, ?, 'SGD', ?, ?, 'SGD Cash', ?)""",
        (account_id, parsed["period_end"], cb["ending_balance"], cb["ending_balance"], source_file),
    )
    n_positions += 1

    # --- Cash-leg transactions (fees, deposits, buys, sells, distributions) ---
    n_cash_txn = 0
    for i, t in enumerate(cb["transactions"]):
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, gross_amount, raw_txn_type_label, raw_description,
                  source_reference, source_file)
               VALUES (?, NULL, 'CASH', ?, ?, ?, 'SGD', ?, ?, ?, ?, ?)""",
            (account_id, t["trade_date"], t["txn_type"], t["txn_subtype"],
             t["gross_amount"], t["raw_label"], t["raw_description"],
             f"{source_file}:CASH:{i}", source_file),
        )
        n_cash_txn += cur.rowcount

    # --- Security-leg transactions (unit-level trade detail per goal) ---
    n_sec_txn = 0
    type_map = {"Investment": "BUY", "Redemption": "SELL", "Distribution reinvested": "BUY", "Fee": "FEE"}
    for i, t in enumerate(parsed["goal_transactions"]):
        instrument_id = None
        for row in parsed["allocations"]:
            if row["source"] != "GOAL_ALLOCATION":
                continue
            if row["fund_name"] and t["details"] and row["fund_name"].startswith(t["details"][:20]):
                instrument_id = row["isin"] or f"NAME:{row['fund_name']}"
                break
        txn_type = type_map.get(t["txn_type_raw"], "OTHER")
        if t["txn_type_raw"] == "Distribution reinvested":
            subtype = "REINVESTMENT"
        elif t["txn_type_raw"] == "Fee":
            # Endowus charges its advisory fee by selling a small unit
            # fraction directly, for goals with no cash pool to deduct from
            # (CPF/SRS) - confirmed via the Single account's statement.
            subtype = "ADVISORY_FEE"
        else:
            subtype = None
        cur = conn.execute(
            """INSERT OR IGNORE INTO txn
                 (account_id, instrument_id, source_leg, trade_date, txn_type, txn_subtype,
                  currency, quantity, price, gross_amount, raw_txn_type_label, raw_description,
                  source_reference, source_file)
               VALUES (?, ?, 'SECURITY', ?, ?, ?, 'SGD', ?, ?, ?, ?, ?, ?, ?)""",
            (account_id, instrument_id, t["trade_date"], txn_type, subtype,
             t["units"], t["price"], t["amount"] * (-1 if txn_type in ("BUY", "FEE") else 1),
             t["txn_type_raw"], t["details"], f"{source_file}:SEC:{i}", source_file),
        )
        n_sec_txn += cur.rowcount

    conn.commit()
    print(f"[endowus] {source_file}: loaded {n_positions} positions, {n_cash_txn} cash txns, "
          f"{n_sec_txn} security txns")

    # --- Performance: a genuine Modified Dietz return for this period, using
    # the statement's own stated starting/ending fund balance (a real BMV,
    # not an assumption) and the real dated buy/sell flows this month. ---
    headline = parsed["headline"]
    if headline.get("starting_balance") and headline.get("ending_balance") and headline.get("start_date"):
        flows = []
        for t in parsed["goal_transactions"]:
            sign = 1 if t["txn_type_raw"] == "Investment" else -1  # Buy=inflow to fund pool, Sell/Redemption=outflow
            flows.append((t["trade_date"], t["amount"] * sign))
        # Deliberately GROSS of fees: fees are paid from cash, not from the
        # fund balance BMV/EMV are based on, so folding a fee into this flow
        # list would be mathematically inconsistent (it would push the
        # computed return the WRONG direction, since Modified Dietz assumes
        # CF_i are flows into/out of the SAME pool whose BMV/EMV is being
        # measured). The fee-inclusive dollar result is Endowus's own
        # "Returns" headline (stored separately below as gain_value) - report
        # both, don't force them into one blended percentage.
        twr = modified_dietz(headline["starting_balance"], headline["ending_balance"], flows,
                              headline["start_date"], headline.get("end_date") or parsed["period_end"])
        conn.execute(
            """INSERT OR IGNORE INTO account_valuation_history
                 (account_id, period_type, period_end, period_label, currency,
                  final_value, inflows, outflows, gain_value, twr_pct, source_file)
               VALUES (?, 'month_end', ?, ?, 'SGD', ?, ?, ?, ?, ?, ?)""",
            (account_id, headline.get("end_date") or parsed["period_end"],
             f"{headline['start_date']} to {headline.get('end_date') or parsed['period_end']}",
             headline["ending_balance"],
             sum(a for _, a in flows if a > 0), sum(a for _, a in flows if a < 0),
             headline.get("returns"), (twr * 100) if twr is not None else None, source_file),
        )
        conn.commit()
        print(f"[endowus] Period return (Modified Dietz, fund-level, gross of fees): "
              f"{twr*100:.2f}%" if twr is not None else "[endowus] Period return: not computable")

    return {"n_positions": n_positions, "n_cash_txn": n_cash_txn, "n_sec_txn": n_sec_txn}


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 2 and sys.argv[2] == "--load":
        from db import get_connection
        conn = get_connection()
        force = "--force" in sys.argv
        load_endowus_statement(sys.argv[1], conn, force=force)
        conn.close()
    else:
        r = parse_statement(sys.argv[1])
        print(json.dumps({
            "period_end": r["period_end"],
            "n_overview_goals": len(r["overview"]),
            "n_allocation_rows": len(r["allocations"]),
            "n_goal_txns": len(r["goal_transactions"]),
            "cash_balance": {k: v for k, v in r["cash_balance"].items() if k != "transactions"},
            "n_cash_txns": len(r["cash_balance"]["transactions"]),
        }, indent=2))
