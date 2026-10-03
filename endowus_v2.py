"""Parser for the Endowus statement layout introduced in August 2026.

Confirmed on real statements (not assumed): from August 2026 both the Joint
and Single statements use a new, much more compact layout - "Goals summary
(by currency / by goal / footnotes)", one "Aggregated asset allocation"
table (by fund and funding source, NOT per goal), one "Completed
transactions for all goals" list, "Cash balance overview" plus a "Cash
balance (SGD)" ledger - and both carry a full text layer. This parser reads
that text directly; it never OCRs. If any page has no text layer it refuses
to parse (OCR'd digits are not trustworthy enough to load: the same July
file read a cash balance as 4,269.38 instead of 1,269.38 under one
Tesseract build while every reconciliation gate still passed).

Output deliberately matches the shape ingest_endowus_pdf.parse_statement()
produces for the older layout, so the existing loader, its reconciliation
gates and its inserts are reused rather than duplicated.

Anything this parser does not recognise raises ValueError instead of being
skipped: an unknown transaction type, an unexpected currency, a column count
that does not line up. Silently dropping a row is the failure mode every
gate in this project exists to prevent.
"""
import re

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
MIN_TEXT_CHARS = 40

FUNDING = r"(?:SGD Cash|USD Cash|CPF OA|CPF SA|SRS)"
ASSET_CLASS = r"(?:Equities|Multi-asset|Fixed income|Alternatives|Commodities|Cash)"
# Normalised to the labels the older layout produced, so instrument.asset_class
# and the Overview grouping (reports.ASSET_CLASS_PARENT) stay consistent.
ASSET_CLASS_NORMALIZED = {"Multi-asset": "Multi Asset", "Fixed income": "Fixed Income"}

ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
DATE_RE = re.compile(r"\b(\d{2}) ([A-Z][a-z]{2}) (\d{4})\b")
FOOTER_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w+ Page \d+ of \d+$")
# Amounts carry a footnote digit glued on ("S$778,059.103" = 778,059.10 + note 3).
# Statement amounts are always 2dp, so a third digit can only be a footnote.
MONEY_TOKEN = re.compile(r"^([+-]?)S\$([\d,]+\.\d\d)\d?$")

# Cash-ledger row types -> (txn_type, txn_subtype); matches the older layout's
# CASH_TXN_TYPES so both layouts land in the database the same way.
LEDGER_TYPES = [
    ("Distribution reinvested", "BUY", "REINVESTMENT"),
    ("Distribution received", "INCOME", "DISTRIBUTION"),
    ("Investment Bought", "BUY", "FUND_PURCHASE"),
    ("Redemption", "SELL", "FUND_REDEMPTION"),
    ("Endowus Fee", "FEE", "ADVISORY_FEE"),
    ("Cashback received", "INCOME", "CASHBACK"),
    ("Deposit", "DEPOSIT", "EXTERNAL_INCOMING"),
    ("Withdrawal", "WITHDRAWAL", "EXTERNAL_OUTGOING"),
]
TXN_RECORD_TYPES = ("Investment", "Redemption", "Distribution reinvested",
                    "Distribution received", "Fee")


def isin_valid(s):
    """ISIN shape AND check digit (Luhn over the letter-expanded digits). Shape
    alone cannot tell letter O from digit 0 - 'IEOOOXNHMJW8' (OCR) has the same
    shape as 'IE000XNHMJW8' (real). Verified on 102 real ISINs from five
    independent sources: all pass; every OCR-garbled variant seen fails."""
    if not s or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", s):
        return False
    total = 0
    for i, ch in enumerate(reversed("".join(str(int(c, 36)) for c in s))):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def is_v2(pages):
    return any(p.startswith(("Goals summary (by goal)", "Aggregated asset allocation"))
               for p in pages)


def _num(s):
    return float(s.replace(",", ""))


def _iso(d, mon, y):
    return f"{y}-{MONTHS[mon]:02d}-{int(d):02d}"


def _money(tok):
    """'+S$1,000.00' -> 1000.0 ; '-S$8.35' -> -8.35 ; '-' -> 0.0 ; else None."""
    if tok == "-":
        return 0.0
    m = MONEY_TOKEN.match(tok)
    if not m:
        return None
    return _num(m.group(2)) * (-1 if m.group(1) == "-" else 1)


def _body(page, skip=3):
    """Page lines without the title/period/description header or the footer."""
    lines = [l.strip() for l in page.split("\n") if l.strip()]
    return [l for l in lines[skip:] if not FOOTER_RE.search(l)]


def _join_name(parts):
    out = ""
    for p in parts:
        if not p:
            continue
        # a wrapped hyphenated name ("SGD-" / "Hedged") joins without a space
        out = (out + p) if out.endswith("-") and not out.endswith(" -") else (out + " " + p if out else p)
    return out.strip()


def _trailing_values(line):
    """Split 'Label S$1.00 - +S$2.00' into ('Label', [1.0, 0.0, 2.0])."""
    parts = line.split(" ")
    vals = []
    while parts:
        v = _money(parts[-1])
        if v is None:
            break
        vals.append(v)
        parts.pop()
    vals.reverse()
    return " ".join(parts), vals


# ---------------------------------------------------------------- cover ----
def _parse_cover(page):
    def grab(label):
        m = re.search(label + r" S\$([\d,]+\.\d\d)", page)
        if not m:
            raise ValueError(f"cover page: could not find '{label}'")
        return _num(m.group(1))
    m = re.search(r"(\d{2}) ([A-Z][a-z]{2}) (\d{4}) to (\d{2}) ([A-Z][a-z]{2}) (\d{4})", page)
    if not m:
        raise ValueError("cover page: could not find the statement period")
    cover = {
        "start_date": _iso(m.group(1), m.group(2), m.group(3)),
        "end_date": _iso(m.group(4), m.group(5), m.group(6)),
        "investment_value": grab("Investment value"),
        "cash_balance": grab("Cash balance"),
        "total_account_value": grab("Total account value"),
    }
    if abs(cover["investment_value"] + cover["cash_balance"] - cover["total_account_value"]) > 0.02:
        raise ValueError("cover page: investment value + cash does not equal total account value")
    return cover


# ------------------------------------------------------- goals summaries ----
METRIC_KEYS = {
    "Starting balance (A)": "A", "Net investment (B)": "B",
    "Investments and transfers in": "B_in", "Redemptions and transfers out": "B_out",
    "Net rebalancing (C)": "C", "Investment gain or loss (D)": "D",
    "Ending balance = A+B+C+D": "E", "Processing transactions": "processing",
    "Distributions paid out (E)": "paid_out", "Other cashflows (F)": "F",
    "Return = D+E+F": "R",
}


def _metric_rows(lines, ncols):
    out = {}
    for line in lines:
        label, vals = _trailing_values(line)
        key = METRIC_KEYS.get(re.sub(r"\d+$", "", label))   # drop footnote digit on the label
        if key is None or not vals:
            continue
        if len(vals) != ncols:
            raise ValueError(f"goals summary: '{label}' has {len(vals)} values for {ncols} goal columns")
        out[key] = vals
    missing = [k for k in ("A", "B", "C", "D", "E") if k not in out]
    if missing:
        raise ValueError(f"goals summary: missing rows {missing}")
    return out


def _parse_by_currency(pages):
    cur_pages = [p for p in pages if p.startswith("Goals summary (by currency)")]
    if len(cur_pages) != 1:
        raise ValueError("expected exactly one 'Goals summary (by currency)' page")
    lines = _body(cur_pages[0])
    currencies = [l for l in lines if re.match(r"^[A-Z]{3}\d?$", l)]
    if [re.sub(r"\d", "", c) for c in currencies] != ["SGD"]:
        raise ValueError(f"only SGD statements are supported, found currency headers {currencies}")
    m = _metric_rows(lines, 1)
    return {k: v[0] for k, v in m.items()}


def _parse_goals(pages):
    overview = {}
    for pno, page in enumerate(pages, 1):
        if not page.startswith("Goals summary (by goal)"):
            continue
        lines = _body(page)
        if len(lines) < 3:
            raise ValueError(f"page {pno}: goals summary page is too short to parse")
        names_line, funding_line = lines[0], lines[1]
        fundings = re.findall(FUNDING, funding_line)
        if not fundings or re.sub(FUNDING, "", funding_line).strip():
            raise ValueError(f"page {pno}: unexpected funding-source line {funding_line!r}")
        n = len(fundings)
        # Goal names are truncated with an ellipsis, and when only some are
        # truncated the boundaries between names are not recoverable from the
        # text alone - use the name only when every one is delimited.
        names = [x.strip() for x in names_line.split("…") if x.strip()]
        use_names = len(names) == n and names_line.count("…") == n
        m = _metric_rows(lines[2:], n)
        for c in range(n):
            label = re.sub(r"\d+$", "", names[c]) if use_names else f"goal p{pno} col{c + 1}"
            key = f"{label} [{fundings[c]}]"
            A, B, C, D, E = (m[k][c] for k in ("A", "B", "C", "D", "E"))
            net_flow = B + C
            overview[key] = {
                "starting": A, "investments": max(net_flow, 0.0), "redemptions": max(-net_flow, 0.0),
                "gain_loss": D, "ending": E, "funding_source": fundings[c],
                "processing": m.get("processing", [0.0] * n)[c],
            }
    if not overview:
        raise ValueError("no 'Goals summary (by goal)' pages found")
    return overview


# ------------------------------------------------------------ allocation ----
ALLOC_FIRST = re.compile(
    rf"^(?P<name>.+?) (?P<ac>{ASSET_CLASS}) (?P<fs>{FUNDING}) (?P<units>[\d,]+(?:\.\d+)?) "
    rf"S\$(?P<nav>[\d,]+(?:\.\d+)?) S\$(?P<avg>[\d,]+(?:\.\d+)?) "
    rf"S\$(?P<val>[\d,]+\.\d\d) (?P<pct>[\d.]+)%$")
ALLOC_TOTAL = re.compile(r"^Total S\$([\d,]+\.\d\d) ([\d.]+)%$")


def _parse_allocation(pages):
    lines = []
    for page in pages:
        if page.startswith("Aggregated asset allocation"):
            lines += [l for l in _body(page)
                      if not l.startswith(("Fund name Asset class", "ISIN source Price date"))]
    rows, stated_total, cur = [], None, None
    for l in lines:
        t = ALLOC_TOTAL.match(l)
        if t:
            stated_total = _num(t.group(1))
            continue
        if l.startswith("Due to rounding"):
            continue
        m = ALLOC_FIRST.match(l)
        if m:
            cur = {"name_parts": [m.group("name")], "isin": None, "price_date": None,
                   "asset_class": ASSET_CLASS_NORMALIZED.get(m.group("ac"), m.group("ac")),
                   "funding_source": m.group("fs"), "units": _num(m.group("units")),
                   "nav": _num(m.group("nav")), "avg_price": _num(m.group("avg")),
                   "value": _num(m.group("val")), "allocation_pct": float(m.group("pct"))}
            rows.append(cur)
            continue
        if cur is None:
            raise ValueError(f"allocation: unexpected line before the first fund row: {l!r}")
        rest = l
        dm = DATE_RE.search(rest)
        if dm:
            cur["price_date"] = _iso(*dm.groups())
            rest = rest.replace(dm.group(0), "")
        im = ISIN_RE.search(rest)
        if im:
            cur["isin"] = im.group(0)
            rest = rest.replace(im.group(0), "")
        rest = rest.strip()
        if rest:
            cur["name_parts"].append(rest)
    if stated_total is None:
        raise ValueError("allocation: no Total row found - table may be incomplete")
    out = []
    for r in rows:
        if not r["isin"]:
            raise ValueError(f"allocation: no ISIN found for {_join_name(r['name_parts'])!r}")
        if not isin_valid(r["isin"]):
            raise ValueError(f"allocation: ISIN {r['isin']!r} for {_join_name(r['name_parts'])!r} "
                             f"fails its check digit - the text is corrupted")
        out.append({
            "goal_name": None, "fund_name": _join_name(r["name_parts"]), "isin": r["isin"],
            "asset_class": r["asset_class"], "units": r["units"], "nav": r["nav"],
            "avg_price": r["avg_price"], "value": r["value"], "allocation_pct": r["allocation_pct"],
            "funding_source": r["funding_source"], "price_date": r["price_date"],
            "source": "AGG_ALLOCATION",
        })
    return out, stated_total


# ---------------------------------------------------------- transactions ----
TXN_HEAD = re.compile(
    rf"^(?P<completed>\d{{2}} [A-Z][a-z]{{2}} \d{{4}})\d? (?P<type>[A-Z][A-Za-z ]+?) – (?P<goal>.+?) "
    rf"(?P<fs>{FUNDING}) (?P<units>[\d,]+(?:\.\d+)?|-) (?P<price>S\$[\d,]+(?:\.\d+)?|-) "
    rf"S\$(?P<amt>[\d,]+\.\d\d)$")


def _parse_transactions(pages):
    lines = []
    for page in pages:
        if page.startswith("Completed transactions for all goals"):
            lines += [l for l in _body(page)
                      if not l.startswith(("Completed date Transaction", "Trade date Price date"))]
    records, cur = [], None
    for l in lines:
        m = TXN_HEAD.match(l)
        if m:
            if m.group("type") not in TXN_RECORD_TYPES:
                raise ValueError(f"unrecognised completed-transaction type {m.group('type')!r}: {l!r}")
            cur = {"head": m, "body": []}
            records.append(cur)
        elif cur is not None:
            cur["body"].append(l)
        elif l != "No activity":
            raise ValueError(f"completed transactions: unexpected line before first record: {l!r}")
    out = []
    for r in records:
        h = r["head"]
        body = " ".join(r["body"])
        dm = DATE_RE.search(body)
        trade_date = _iso(*dm.groups()) if dm else _iso(*DATE_RE.search(h.group("completed")).groups())
        im = re.search(r"\(([A-Z]{2}[A-Z0-9]{9}\d)\)", body)
        side = re.search(r"\b(Buy|Sell)\b", body)
        fund = None
        if side:
            tail = body[side.end():]
            tail = tail.split("(" + (im.group(1) if im else "\0") + ")")[0]
            tail = DATE_RE.sub("", tail)
            tail = re.sub(r"-\s+(?=[A-Z])", "-", tail)
            fund = re.sub(r"\s+", " ", tail).strip() or None
        if im and not isin_valid(im.group(1)):
            raise ValueError(f"transaction ISIN {im.group(1)!r} fails its check digit: {h.group(0)!r}")
        if h.group("type") != "Distribution received" and not (im and side):
            raise ValueError(f"transaction without a recognisable fund/ISIN/side: {h.group(0)!r} / {body!r}")
        units = None if h.group("units") == "-" else _num(h.group("units"))
        price = None if h.group("price") == "-" else _num(h.group("price")[2:])
        out.append({
            "goal_name": h.group("goal"), "trade_date": trade_date, "txn_type_raw": h.group("type"),
            "side": side.group(1) if side else None, "details": fund, "isin": im.group(1) if im else None,
            "funding_source": h.group("fs"), "units": units, "price": price,
            "amount": _num(h.group("amt")),
        })
    return out


# ------------------------------------------------------------------ cash ----
LEDGER_ROW = re.compile(r"^(\d{2}) ([A-Z][a-z]{2}) (\d{4}) (.+) ([+-])S\$([\d,]+\.\d\d)$")


def _parse_cash(pages, period_start, period_end):
    ov = [p for p in pages if p.startswith("Cash balance overview")]
    if len(ov) != 1:
        raise ValueError("expected exactly one 'Cash balance overview' page")
    accts = []
    for l in _body(ov[0]):
        m = re.match(r"^([A-Z]{3}) Cash S\$([\d,]+\.\d\d) S\$([\d,]+\.\d\d)$", l)
        if m:
            accts.append((m.group(1), _num(m.group(2)), _num(m.group(3))))
    if [a[0] for a in accts] != ["SGD"]:
        raise ValueError(f"only an SGD cash account is supported, found {[a[0] for a in accts]}")
    _, start, end = accts[0]

    ledger = [p for p in pages if p.startswith("Cash balance (")]
    if any(not p.startswith("Cash balance (SGD)") for p in ledger):
        raise ValueError("cash ledger in a currency other than SGD is not supported")
    txns = []
    if not ledger:
        if abs(start - end) > 0.005:
            raise ValueError(f"cash moved {start:.2f} -> {end:.2f} but no ledger pages were found")
        return {"starting_balance": start, "ending_balance": end, "start_date": period_start,
                "end_date": period_end, "transactions": txns}

    cur = None
    for page in ledger:
        for l in _body(page):
            if l.startswith(("Movements affecting", "Trade date Transaction", "Starting balance",
                             "Ending balance")):
                continue
            m = LEDGER_ROW.match(l)
            if m:
                d, mon, y, desc, sign, amt = m.groups()
                for prefix, txn_type, subtype in LEDGER_TYPES:
                    if desc.startswith(prefix):
                        break
                else:
                    raise ValueError(f"unrecognised cash-ledger row type: {l!r}")
                cur = {"trade_date": _iso(d, mon, y), "raw_label": prefix, "txn_type": txn_type,
                       "txn_subtype": subtype, "raw_description": desc,
                       "gross_amount": _num(amt) * (1 if sign == "+" else -1)}
                txns.append(cur)
            elif cur is not None:
                cur["raw_description"] += " " + l       # wrapped continuation of the previous row
            else:
                raise ValueError(f"cash ledger: unexpected line before first row: {l!r}")
    full = "\n".join(ledger)
    s = re.search(r"Starting balance S\$([\d,]+\.\d\d)", full)
    e = re.findall(r"Ending balance S\$([\d,]+\.\d\d)", full)
    if not s or not e:
        raise ValueError("cash ledger: starting/ending balance line not found")
    if abs(_num(s.group(1)) - start) > 0.005 or abs(_num(e[-1]) - end) > 0.005:
        raise ValueError("cash ledger balances disagree with the cash balance overview")
    return {"starting_balance": start, "ending_balance": end, "start_date": period_start,
            "end_date": period_end, "transactions": txns}


# ------------------------------------------------------------------ main ----
def parse_v2(pages, source_file):
    empty = [i + 1 for i, p in enumerate(pages) if len(p.strip()) < MIN_TEXT_CHARS]
    if empty:
        raise ValueError(
            f"pages {empty} have no text layer. The August-2026-onward layout is only parsed "
            f"from embedded text - never OCR - so this file cannot be loaded as-is.")
    cover = _parse_cover(pages[0])
    cur = _parse_by_currency(pages)
    overview = _parse_goals(pages)
    allocations, alloc_total = _parse_allocation(pages)
    cash = _parse_cash(pages, cover["start_date"], cover["end_date"])
    cover["allocation_total"] = alloc_total
    headline = {
        "start_date": cover["start_date"], "end_date": cover["end_date"],
        "starting_balance": cur["A"], "ending_balance": cur["E"],
        "returns": cur.get("R"), "investment_value": cover["investment_value"],
        "net_investment": cur["B"] + cur["C"],
    }
    return {
        "format": "v2", "period_end": cover["end_date"], "overview": overview,
        "allocations": allocations, "goal_transactions": _parse_transactions(pages),
        "cash_balance": cash, "headline": headline, "cover": cover, "source_file": source_file,
    }
