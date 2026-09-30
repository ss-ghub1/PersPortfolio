"""
Parses the "2025: Monthly net performance" and "20XX-20XX: Annual net
performance" tables out of a UBS "Statement of assets" PDF. These tables
are UBS's OWN computed TWR, with real month-end net asset values going
back to account inception - exactly the historical BMV/EMV series our
transaction-derived Modified Dietz calc couldn't reconstruct on its own
(see performance.py's caveats).

Column positions vary slightly page to page (different currencies/margins),
so boundaries are detected per-page from the header row ("Period Final
value Inflows Outflows Value TWR Value TWR") rather than hardcoded.
"""
import re
from pathlib import Path
import pdfplumber

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}


def _to_number(s):
    if s is None or s == "":
        return None
    s = s.strip().replace(" ", "").rstrip("%")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _group_lines(words):
    rows = {}
    for w in words:
        top = round(w["top"])
        # merge lines within 2px (float rounding jitter)
        key = next((t for t in rows if abs(t - top) <= 2), top)
        rows.setdefault(key, []).append(w)
    return dict(sorted(rows.items()))


def _find_header_anchors(lines, min_top=0):
    """Returns (top, [x0 for Period, Final value, Inflows, Outflows, Value, TWR, Value, TWR])
    for the first header row (at or after min_top) matching the performance table
    header, or None. min_top lets a caller skip past an earlier section's header
    on the same page (e.g. the monthly table's tail and the annual table can share
    a page, each with their own header row)."""
    for top, ws in lines.items():
        if top < min_top:
            continue
        texts = [w["text"] for w in ws]
        if texts[:2] == ["Period", "Final"] and "Inflows" in texts and "Outflows" in texts:
            period_x = ws[0]["x0"]
            final_x = ws[1]["x0"]
            inflows_x = next(w["x0"] for w in ws if w["text"] == "Inflows")
            outflows_x = next(w["x0"] for w in ws if w["text"] == "Outflows")
            value_xs = [w["x0"] for w in ws if w["text"] == "Value"]
            twr_xs = [w["x0"] for w in ws if w["text"] == "TWR"]
            if len(value_xs) >= 2 and len(twr_xs) >= 2:
                anchors = [period_x, final_x, inflows_x, outflows_x,
                           value_xs[0], twr_xs[0], value_xs[1], twr_xs[1]]
                return top, anchors
    return None


def _bucket_row(ws, boundaries):
    """boundaries: list of (col_index, left, right). Returns dict col_index -> joined text."""
    cols = {i: [] for i in range(len(boundaries))}
    for w in ws:
        for i, (lo, hi) in enumerate(boundaries):
            if lo <= w["x0"] < hi:
                cols[i].append(w)
                break
    out = {}
    for i, ws_in_col in cols.items():
        ws_in_col.sort(key=lambda w: w["x0"])
        if i == 0:  # Period column: keep spaces (date text)
            out[i] = " ".join(w["text"] for w in ws_in_col)
        else:  # numeric columns: strip spaces (thousand separators)
            out[i] = "".join(w["text"] for w in ws_in_col)
    return out


def _parse_period_label(label):
    """Returns (period_type, iso_date_or_None, display_label)."""
    label = label.strip()
    if label == "Cumulative":
        return "cumulative", None, label
    m = re.match(r"^(\d{1,2}) (\w+) (\d{4})$", label)
    if m:
        day, month_name, year = m.groups()
        month_name = month_name.rstrip(",")
        if month_name in MONTH_NUM:
            iso = f"{year}-{MONTH_NUM[month_name]:02d}-{int(day):02d}"
            return "month_end", iso, label
    # annual row: a bare year, possibly with a footnote digit stuck on, e.g. "20201"
    m = re.match(r"^(20\d{2})(\d?)$", label)
    if m:
        year = m.group(1)
        return "year_end", f"{year}-12-31", year
    return "unknown", None, label


def _extract_table_rows(page, min_top, stop_pred):
    lines = _group_lines(page.extract_words())
    header = _find_header_anchors(lines, min_top=min_top)
    if not header:
        return []
    header_top, anchors = header
    boundaries = []
    for i in range(len(anchors)):
        lo = anchors[i] - 5 if i == 0 else (anchors[i - 1] + anchors[i]) / 2
        hi = (anchors[i] + anchors[i + 1]) / 2 if i < len(anchors) - 1 else anchors[i] + 120
        boundaries.append((lo, hi))

    rows = []
    for top, ws in lines.items():
        if top <= header_top:
            continue
        texts = [w["text"] for w in ws]
        line_text = " ".join(texts)
        if stop_pred(line_text):
            break
        if "Reference currency" in line_text:
            continue
        cols = _bucket_row(ws, boundaries)
        period_type, iso_date, label = _parse_period_label(cols.get(0, ""))
        if period_type == "unknown":
            continue
        rows.append({
            "period_type": period_type,
            "period_end": iso_date,
            "period_label": label,
            "final_value": _to_number(cols.get(1)),
            "inflows": _to_number(cols.get(2)),
            "outflows": _to_number(cols.get(3)),
            "gain_value": _to_number(cols.get(4)),
            "twr_pct": _to_number(cols.get(5)),
            "cum_value": _to_number(cols.get(6)),
            "cum_twr_pct": _to_number(cols.get(7)),
        })
    return rows


def parse_statement_pdf(path):
    """Returns dict: {native_account_code, currency, as_of_date, monthly: [...], annual: [...]}."""
    path = Path(path)
    result = {"native_account_code": None, "currency": None, "as_of_date": None,
              "monthly": [], "annual": [], "source_file": path.name}

    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)

        m = re.search(r"Portfolio number ([\d\- ]+\d)", full_text)
        if m:
            code = m.group(1).replace("-", " ").strip()
            # normalize '546 337340 02' -> '0546 00337340 0002' style used elsewhere?
            # Keep as UBS prints it; caller maps via account table's native_account_code.
            result["native_account_code"] = code
        m = re.search(r"valued in (\w+) \(([A-Z]{3})\)|Valued in ([A-Z]{3})", full_text)
        m2 = re.search(r"\(([A-Z]{3})\)", full_text)
        if m2:
            result["currency"] = m2.group(1)
        m3 = re.search(r"valued as of (\d{2})\.(\d{2})\.(\d{4})", full_text)
        if m3:
            d, mth, y = m3.groups()
            result["as_of_date"] = f"{y}-{mth}-{d}"

        for page in pdf.pages:
            words = page.extract_words()
            lines = _group_lines(words)

            # Find every header row on this page (there can be two: the tail of a
            # monthly table continuing from the previous page, and the annual
            # table, both on the same page) and classify each by the nearest
            # section-title text appearing just above it.
            header_tops = []
            search_from = 0
            while True:
                found = _find_header_anchors(lines, min_top=search_from)
                if not found:
                    break
                top, _ = found
                header_tops.append(top)
                search_from = top + 1

            for idx, header_top in enumerate(header_tops):
                window_start = header_tops[idx - 1] if idx > 0 else 0
                nearby_text = " ".join(
                    w["text"] for top, ws in lines.items() if window_start <= top < header_top
                    for w in ws
                )
                next_header_top = header_tops[idx + 1] if idx + 1 < len(header_tops) else None
                if "Annual net performance" in nearby_text:
                    rows = _extract_table_rows(
                        page, min_top=header_top,
                        stop_pred=lambda t, nh=next_header_top: ("Portfolio investment profile" in t)
                                  or ("History:" in t),
                    )
                    result["annual"].extend(rows)
                elif "Monthly net performance" in nearby_text:
                    rows = _extract_table_rows(
                        page, min_top=header_top,
                        stop_pred=lambda t: ("Annual net performance" in t) or ("Portfolio investment profile" in t)
                                  or ("Details of income" in t),
                    )
                    result["monthly"].extend(rows)

    return result


def _account_code_parts(native_code):
    """'0546 00337340 0001' or '546 337340 01' -> ('546', '337340', '1'),
    stripping leading zeros so differently-padded formats compare equal."""
    parts = native_code.strip().split()
    stripped = [p.lstrip("0") or "0" for p in parts]
    return tuple(stripped[-3:]) if len(stripped) >= 3 else tuple(stripped)


def load_statement_pdf(path, conn):
    """Parses a UBS statement PDF and loads its monthly + annual UBS-computed
    TWR history into account_valuation_history."""
    parsed = parse_statement_pdf(path)

    target_parts = _account_code_parts(parsed["native_account_code"] or "")
    candidates = conn.execute(
        "SELECT account_id, native_account_code FROM account WHERE institution_id='UBS'"
    ).fetchall()
    account_id = None
    for r in candidates:
        if _account_code_parts(r["native_account_code"]) == target_parts:
            account_id = r["account_id"]
            break
    if not account_id:
        print(f"[statement pdf] WARNING: could not map account code "
              f"'{parsed['native_account_code']}' to a known account - skipped.")
        return 0

    n_loaded = 0
    for section, rows in (("monthly", parsed["monthly"]), ("annual", parsed["annual"])):
        for r in rows:
            period_end = r["period_end"]
            period_type = r["period_type"]
            if period_type == "cumulative":
                # the since-inception summary row has no single date of its own -
                # anchor it to the statement's as-of date so it's still queryable,
                # tagged distinctly so it's never confused with a real month/year row.
                if not parsed["as_of_date"]:
                    continue
                period_end = parsed["as_of_date"]
                period_type = f"cumulative_{section}"  # cumulative_monthly (this-year-to-date) vs cumulative_annual (since inception)
            if period_end is None:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO account_valuation_history
                     (account_id, period_type, period_end, period_label, currency,
                      final_value, inflows, outflows, gain_value, twr_pct,
                      cum_value, cum_twr_pct, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, period_type, period_end, r["period_label"],
                 parsed["currency"], r["final_value"], r["inflows"], r["outflows"],
                 r["gain_value"], r["twr_pct"], r["cum_value"], r["cum_twr_pct"],
                 parsed["source_file"]),
            )
            n_loaded += cur.rowcount
    conn.commit()
    print(f"[statement pdf] {parsed['source_file']} -> {account_id}: loaded {n_loaded} "
          f"valuation history rows ({len(parsed['monthly'])} monthly, {len(parsed['annual'])} annual)")
    return n_loaded


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 2 and sys.argv[2] == "--load":
        from db import get_connection
        conn = get_connection()
        load_statement_pdf(sys.argv[1], conn)
        conn.close()
    else:
        r = parse_statement_pdf(sys.argv[1])
        print(json.dumps(r, indent=2))
