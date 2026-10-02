"""
Data-fetching functions for the web app. Deliberately separate from app.py:
these return plain dicts/lists, not HTML - app.py's routes call these and
render templates around the result. When Phase 2 adds JSON endpoints for
an interactive dashboard, they call the exact same functions and just
jsonify() the result instead of rendering a template - no logic duplicated.
"""
from db import get_connection, load_ownership, CONFIG_DIR


def get_fx_rate(conn, from_ccy, to_ccy="SGD"):
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


def ownership_weight(ownership: dict, account_id: str, owner: str):
    """Returns the owner's fractional ownership (0.0-1.0), or None if the
    account has no ownership entry at all (unknown - caller must decide how
    to handle, never silently defaulted)."""
    entries = ownership.get(account_id)
    if entries is None:
        return None
    for e in entries:
        if e["owner"] == owner:
            return e["pct"] / 100.0
    return 0.0  # account IS in config, this owner just isn't listed on it


def list_owners(ownership: dict):
    owners = set()
    for entries in ownership.values():
        for e in entries:
            owners.add(e["owner"])
    return sorted(owners)


def get_account_values(conn, owner=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    owner=None -> consolidated (full value, no weighting).
    owner="SS"/"SV" -> each account's value weighted by that owner's %,
    with accounts missing from account_ownership.json EXCLUDED (not
    zero-filled, not defaulted) and listed separately so the caller can
    surface that gap rather than silently under-count."""
    ownership = load_ownership()
    accounts = conn.execute(
        """SELECT account_id, institution_id, display_code, account_label,
                  base_currency, funding_source FROM account
           ORDER BY institution_id, account_id"""
    ).fetchall()

    rows, total_sgd, unknown = [], 0.0, []
    for a in accounts:
        snap = conn.execute(
            """SELECT sum(market_value_base) mv, max(as_of_date) as_of
               FROM position_snapshot WHERE account_id=?
                 AND as_of_date=(SELECT max(as_of_date) FROM position_snapshot WHERE account_id=?)""",
            (a["account_id"], a["account_id"]),
        ).fetchone()
        native_value = snap["mv"] or 0.0
        rate = get_fx_rate(conn, a["base_currency"])
        value_sgd = native_value * rate if rate else None

        weight = 1.0
        if owner:
            w = ownership_weight(ownership, a["account_id"], owner)
            if w is None:
                unknown.append(a["account_id"])
                continue
            weight = w

        weighted_sgd = (value_sgd * weight) if value_sgd is not None else None
        rows.append({
            "account_id": a["account_id"], "institution_id": a["institution_id"],
            "display_code": a["display_code"] or a["account_id"],
            "account_label": a["account_label"], "base_currency": a["base_currency"],
            "funding_source": a["funding_source"], "as_of": snap["as_of"],
            "native_value": native_value, "value_sgd": weighted_sgd,
            "ownership_weight": weight,
        })
        if weighted_sgd is not None:
            total_sgd += weighted_sgd

    rows.sort(key=lambda r: (r["institution_id"], r["account_id"]))
    return {"rows": rows, "total_sgd": total_sgd, "unknown_ownership_accounts": unknown}


# Every institution labels asset classes differently (UBS: "Equities -
# Equity investments", CDP: "Equities", Endowus: "Equity Fund"). Maps each
# raw label actually seen in the data to a parent category. A label NOT in
# this dict falls into "Other" with its own sub-label preserved, rather
# than silently disappearing - same never-drop-data principle used
# throughout this project. If a new institution introduces an asset_class
# label not listed here, it shows up under "Other" (visible, not hidden)
# until this mapping is extended to cover it explicitly.
ASSET_CLASS_PARENT = {
    "Equities": "Equities", "Equities - Equity investments": "Equities", "Equity Fund": "Equities",
    "Bonds": "Bonds & Fixed Income", "Bonds - Bond investments": "Bonds & Fixed Income",
    "Fixed Income": "Bonds & Fixed Income",
    "Cash": "Cash & Money Market", "Liquidity - Money market investments": "Cash & Money Market",
    "Multi Asset": "Multi Asset",
    "Real estate - Real estate investments": "Real Estate",
    "Hedge funds & private markets - Hedge funds": "Hedge Funds & Private Markets",
    "Hedge funds & private markets - Private markets": "Hedge Funds & Private Markets",
    "Insurance/Fixed Deposits": "Insurance & Fixed Deposits",
    "Others - Asset allocation funds": "Other",
}


def get_asset_allocation(conn, owner=None):
    """Returns {"groups": [{parent, value_sgd, pct, subs: [{label, value_sgd,
    pct_of_parent}, ...]}, ...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Same owner-weighting logic as get_account_values, grouped into parent
    categories (see ASSET_CLASS_PARENT) with sub-labels shown within each."""
    ownership = load_ownership()
    rows = conn.execute(
        """SELECT p.account_id, a.base_currency, COALESCE(i.asset_class, 'Cash') AS asset_class,
                  sum(p.market_value_base) AS mv
           FROM position_snapshot p
           JOIN account a ON a.account_id = p.account_id
           LEFT JOIN instrument i ON i.instrument_id = p.instrument_id
           WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2
                                  WHERE p2.account_id = p.account_id)
           GROUP BY p.account_id, a.base_currency, asset_class"""
    ).fetchall()

    sub_totals, unknown = {}, set()
    for r in rows:
        weight = 1.0
        if owner:
            w = ownership_weight(ownership, r["account_id"], owner)
            if w is None:
                unknown.add(r["account_id"])
                continue
            weight = w
        rate = get_fx_rate(conn, r["base_currency"])
        if rate is None:
            continue
        val_sgd = (r["mv"] or 0) * rate * weight
        raw_label = r["asset_class"]
        parent = ASSET_CLASS_PARENT.get(raw_label, "Other")
        key = (parent, raw_label)
        sub_totals[key] = sub_totals.get(key, 0.0) + val_sgd

    grand_total = sum(sub_totals.values())

    by_parent = {}
    for (parent, raw_label), val in sub_totals.items():
        by_parent.setdefault(parent, []).append({"label": raw_label, "value_sgd": val})

    groups = []
    for parent, subs in by_parent.items():
        parent_total = sum(s["value_sgd"] for s in subs)
        for s in subs:
            s["pct_of_parent"] = (s["value_sgd"] / parent_total * 100) if parent_total else 0
        subs.sort(key=lambda s: -s["value_sgd"])
        groups.append({
            "parent": parent, "value_sgd": parent_total,
            "pct": (parent_total / grand_total * 100) if grand_total else 0,
            "subs": subs,
        })
    groups.sort(key=lambda g: -g["value_sgd"])

    return {"groups": groups, "total_sgd": grand_total, "unknown_ownership_accounts": sorted(unknown)}


def get_positions(conn, owner=None, institution_filter=None, account_filter=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    One row per holding (latest snapshot per account), institution/account
    filters optional. Same owner-weighting rules as get_account_values -
    an account missing from account_ownership.json is excluded (not
    zero-filled) from an owner's view, never silently defaulted."""
    ownership = load_ownership()
    query = """
        SELECT p.account_id, p.instrument_id, p.is_cash, p.as_of_date,
               p.position_currency, p.quantity, p.quantity_on_loan,
               p.market_value_native, p.market_value_base, p.funding_source,
               i.name AS instrument_name, i.asset_class,
               a.institution_id, a.display_code, a.account_label, a.base_currency
        FROM position_snapshot p
        JOIN account a ON a.account_id = p.account_id
        LEFT JOIN instrument i ON i.instrument_id = p.instrument_id
        WHERE p.as_of_date = (SELECT max(as_of_date) FROM position_snapshot p2
                               WHERE p2.account_id = p.account_id)
    """
    params = []
    if institution_filter:
        query += " AND a.institution_id = ?"
        params.append(institution_filter)
    if account_filter:
        query += " AND p.account_id = ?"
        params.append(account_filter)

    rows, total_sgd, unknown = [], 0.0, set()
    for r in conn.execute(query, params).fetchall():
        weight = 1.0
        if owner:
            w = ownership_weight(ownership, r["account_id"], owner)
            if w is None:
                unknown.add(r["account_id"])
                continue
            weight = w

        rate = get_fx_rate(conn, r["base_currency"])
        value_sgd = (r["market_value_base"] * rate * weight) if rate and r["market_value_base"] else None

        rows.append({
            "account_id": r["account_id"], "institution_id": r["institution_id"],
            "display_code": r["display_code"] or r["account_id"],
            "instrument_name": r["instrument_name"] or ("Cash" if r["is_cash"] else r["instrument_id"]),
            "asset_class": r["asset_class"] or ("Cash" if r["is_cash"] else None),
            "is_cash": bool(r["is_cash"]),
            "currency": r["position_currency"], "quantity": r["quantity"],
            "quantity_on_loan": r["quantity_on_loan"],
            "funding_source": r["funding_source"],
            "value_native": r["market_value_native"], "value_base": r["market_value_base"],
            "value_sgd": value_sgd, "as_of": r["as_of_date"],
        })
        if value_sgd is not None:
            total_sgd += value_sgd

    rows.sort(key=lambda r: (r["institution_id"], r["account_id"], -(r["value_sgd"] or 0)))
    return {"rows": rows, "total_sgd": total_sgd, "unknown_ownership_accounts": sorted(unknown)}


def list_txn_types(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT txn_type FROM txn ORDER BY txn_type")]


def get_transactions(conn, owner=None, institution_filter=None, account_filter=None,
                      txn_type_filter=None, date_from=None, date_to=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Raw transaction listing - both SECURITY and CASH legs shown as distinct
    rows (unlike get_fees/get_income, this doesn't dedupe between legs,
    since this is a listing of what's actually in the database, not an
    aggregate total where double-counting would matter)."""
    ownership = load_ownership()
    query = """
        SELECT t.account_id, t.source_leg, t.trade_date, t.txn_type, t.txn_subtype,
               t.currency, t.quantity, t.price, t.gross_amount, t.raw_description,
               t.raw_txn_type_label, i.name AS instrument_name,
               a.institution_id, a.display_code
        FROM txn t
        JOIN account a ON a.account_id = t.account_id
        LEFT JOIN instrument i ON i.instrument_id = t.instrument_id
        WHERE 1=1
    """
    params = []
    if institution_filter:
        query += " AND a.institution_id = ?"
        params.append(institution_filter)
    if account_filter:
        query += " AND t.account_id = ?"
        params.append(account_filter)
    if txn_type_filter:
        query += " AND t.txn_type = ?"
        params.append(txn_type_filter)
    if date_from:
        query += " AND t.trade_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND t.trade_date <= ?"
        params.append(date_to)
    query += " ORDER BY t.trade_date DESC, t.account_id"

    rows, total_sgd, unknown = [], 0.0, set()
    for r in conn.execute(query, params).fetchall():
        weight = 1.0
        if owner:
            w = ownership_weight(ownership, r["account_id"], owner)
            if w is None:
                unknown.add(r["account_id"])
                continue
            weight = w

        rate = get_fx_rate(conn, r["currency"])
        amount_sgd = (r["gross_amount"] * rate * weight) if rate and r["gross_amount"] is not None else None

        rows.append({
            "account_id": r["account_id"], "institution_id": r["institution_id"],
            "display_code": r["display_code"] or r["account_id"],
            "source_leg": r["source_leg"], "trade_date": r["trade_date"],
            "txn_type": r["txn_type"], "txn_subtype": r["txn_subtype"],
            "instrument_name": r["instrument_name"],
            "description": r["raw_description"] or r["raw_txn_type_label"],
            "currency": r["currency"], "quantity": r["quantity"], "price": r["price"],
            "amount_native": r["gross_amount"], "amount_sgd": amount_sgd,
        })
        if amount_sgd is not None:
            total_sgd += amount_sgd

    return {"rows": rows, "total_sgd": total_sgd, "unknown_ownership_accounts": sorted(unknown)}


def _leg_preferred_query(txn_type):
    """Shared CASH-preferred/SECURITY-fallback pattern, ported from
    demo_reports.py: avoids double-counting fees/income that exist on both
    legs for most institutions, while still picking up accounts (e.g.
    Endowus Single's CPF/SRS goals) where SECURITY is the only leg that
    carries this txn_type at all."""
    return f"""
        AND (t.source_leg = 'CASH'
             OR (t.source_leg = 'SECURITY' AND NOT EXISTS (
                   SELECT 1 FROM txn t2 WHERE t2.account_id = t.account_id
                     AND t2.txn_type = '{txn_type}' AND t2.source_leg = 'CASH')))
    """


def get_fees(conn, owner=None, institution_filter=None, date_from=None, date_to=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Grouped by account + subtype + year."""
    return _grouped_cash_report(conn, "FEE", owner,
                                 group_cols=["t.txn_subtype", "substr(t.trade_date,1,4)"],
                                 group_labels=["txn_subtype", "year"],
                                 institution_filter=institution_filter, date_from=date_from, date_to=date_to)


def get_income(conn, owner=None, institution_filter=None, date_from=None, date_to=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Grouped by account + year + subtype."""
    return _grouped_cash_report(conn, "INCOME", owner,
                                 group_cols=["substr(t.trade_date,1,4)", "t.txn_subtype"],
                                 group_labels=["year", "txn_subtype"],
                                 institution_filter=institution_filter, date_from=date_from, date_to=date_to)


def _grouped_cash_report(conn, txn_type, owner, group_cols, group_labels=None,
                          institution_filter=None, date_from=None, date_to=None):
    group_labels = group_labels or [c.split(".")[-1] for c in group_cols]
    ownership = load_ownership()
    params = []
    extra_where = ""
    if institution_filter:
        extra_where += " AND a.institution_id = ?"
        params.append(institution_filter)
    if date_from:
        extra_where += " AND t.trade_date >= ?"
        params.append(date_from)
    if date_to:
        extra_where += " AND t.trade_date <= ?"
        params.append(date_to)
    query = f"""
        SELECT t.account_id, a.institution_id, a.display_code,
               {', '.join(f'{c} AS {l}' for c, l in zip(group_cols, group_labels))},
               t.currency, round(sum(t.gross_amount), 2) AS total, count(*) n
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = '{txn_type}'
        {_leg_preferred_query(txn_type)}
        {extra_where}
        GROUP BY t.account_id, {', '.join(group_cols)}, t.currency
        ORDER BY a.institution_id, a.account_id
    """
    rows, total_sgd, unknown = [], 0.0, set()
    for r in conn.execute(query, params).fetchall():
        weight = 1.0
        if owner:
            w = ownership_weight(ownership, r["account_id"], owner)
            if w is None:
                unknown.add(r["account_id"])
                continue
            weight = w
        rate = get_fx_rate(conn, r["currency"])
        value_sgd = (r["total"] * rate * weight) if rate else None
        row = {
            "account_id": r["account_id"], "institution_id": r["institution_id"],
            "display_code": r["display_code"] or r["account_id"],
            "currency": r["currency"], "amount_native": r["total"], "n": r["n"],
            "amount_sgd": value_sgd,
        }
        for label in group_labels:
            row[label] = r[label]
        rows.append(row)
        if value_sgd is not None:
            total_sgd += value_sgd
    return {"rows": rows, "total_sgd": total_sgd, "unknown_ownership_accounts": sorted(unknown)}


def load_known_gaps() -> dict:
    """Returns {(account_id, scope): note}. Explicit config only - a
    MISMATCH only shows as 'Known Gap' if its exact account+scope is
    listed here; anything else defaults to 'Review', same explicit-over-
    inferred principle as confirmed_transfers.csv and
    account_ownership.json elsewhere in this project."""
    import csv
    path = CONFIG_DIR / "known_reconciliation_gaps.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[(row["account_id"], row["scope"])] = row["note"]
    return out


def get_reconciliation_status(conn):
    """Returns {"mismatches": [...], "matched": [...]} - mismatches first
    (Known Gap and Review both), matched (OK) grouped separately below,
    so anything needing attention is never buried under a long list of
    passing checks. Same row shape in both lists: {account_id,
    display_code, institution_id, scope, status, label, expected_value,
    actual_value, difference, period_end, notes, run_at}. label is 'OK',
    'MISMATCH / Known Gap', or 'MISMATCH / Review' - MISMATCH itself is
    never softened, the suffix is purely our own classification layered
    on top. Latest run per (account_id, scope) only, not full history."""
    known_gaps = load_known_gaps()
    rows = conn.execute("""
        SELECT r.*, a.display_code, a.institution_id
        FROM reconciliation_log r
        JOIN account a ON a.account_id = r.account_id
        WHERE r.recon_id IN (SELECT max(recon_id) FROM reconciliation_log GROUP BY account_id, scope)
        ORDER BY a.institution_id, r.account_id, r.scope
    """).fetchall()
    mismatches, matched = [], []
    for r in rows:
        status = r["status"]
        if status == "MISMATCH":
            key = (r["account_id"], r["scope"])
            if key in known_gaps:
                label = "MISMATCH / Known Gap"
                gap_note = known_gaps[key]
            else:
                label = "MISMATCH / Review"
                gap_note = None
        else:
            label = status
            gap_note = None
        row = {
            "account_id": r["account_id"], "display_code": r["display_code"] or r["account_id"],
            "institution_id": r["institution_id"], "scope": r["scope"], "status": status,
            "label": label, "expected_value": r["expected_value"], "actual_value": r["actual_value"],
            "difference": r["difference"], "period_end": r["period_end"],
            "notes": r["notes"], "gap_note": gap_note, "run_at": r["run_at"],
        }
        (mismatches if status == "MISMATCH" else matched).append(row)
    # within mismatches, surface unexplained ones before known/explained ones
    mismatches.sort(key=lambda r: r["label"] == "MISMATCH / Known Gap")
    return {"mismatches": mismatches, "matched": matched}


def get_review_queue(conn):
    """Returns list of unclassified transactions - every field needed to
    actually classify them by hand, not just a count."""
    rows = conn.execute("""
        SELECT t.account_id, a.display_code, a.institution_id, t.trade_date,
               t.currency, t.gross_amount, t.raw_description, t.raw_txn_type_label,
               t.source_file
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = 'OTHER' AND t.txn_subtype = 'UNCLASSIFIED'
        ORDER BY t.trade_date DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_data_freshness(conn):
    """Returns list of {account_id, display_code, institution_id,
    last_position_date, last_loaded_at} - one row per account."""
    rows = conn.execute("""
        SELECT a.account_id, a.display_code, a.institution_id,
               max(p.as_of_date) AS last_position_date, max(p.loaded_at) AS last_loaded_at
        FROM account a LEFT JOIN position_snapshot p ON p.account_id = a.account_id
        GROUP BY a.account_id ORDER BY a.institution_id, a.account_id
    """).fetchall()
    return [dict(r) for r in rows]


def get_ownership_coverage(conn):
    """Returns list of account_ids present in accounts.csv but missing
    from account_ownership.json - these silently vanish from every
    owner-filtered view across the app without this check."""
    ownership = load_ownership()
    all_accounts = [r[0] for r in conn.execute("SELECT account_id FROM account ORDER BY account_id")]
    return [a for a in all_accounts if a not in ownership]


def _prior_month_key(as_of_date):
    """'2026-09-18' -> '2026-08' (the calendar month immediately before)."""
    y, m = int(as_of_date[:4]), int(as_of_date[5:7])
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def get_prior_month_values(conn, owner=None):
    """Returns {account_id: value_sgd_or_None}. value_sgd is None when that
    account's prior calendar month hasn't been loaded at all (shown as "-"
    by the caller, not silently treated as zero).

    UBS: from account_valuation_history (its own month-end TWR data,
    separate from the position snapshot - see the Overview design notes).
    Every other institution: from position_snapshot, since each monthly
    statement IS the position snapshot for that institution.

    Uses the latest available FX rate for conversion, same as every other
    SGD figure in this app - NOT the rate that was actually in effect
    during that prior month. Only UBS's position-snapshot loader ever
    captures a dated FX rate at all, and since that's a single "current
    state" file typically reloaded repeatedly rather than an accumulating
    monthly archive, a true historical rate usually isn't available
    anyway - this is a deliberate, acknowledged simplification, not an
    oversight. A currency-heavy account's apparent month-over-month change
    can therefore be partly FX movement, not purely portfolio movement."""
    ownership = load_ownership()
    accounts = conn.execute(
        """SELECT account_id, institution_id, base_currency FROM account ORDER BY account_id"""
    ).fetchall()

    out = {}
    for a in accounts:
        latest = conn.execute(
            "SELECT max(as_of_date) FROM position_snapshot WHERE account_id=?",
            (a["account_id"],),
        ).fetchone()[0]
        if not latest:
            out[a["account_id"]] = None
            continue
        prior_key = _prior_month_key(latest)

        if a["institution_id"] == "UBS":
            row = conn.execute(
                """SELECT final_value FROM account_valuation_history
                   WHERE account_id=? AND period_type='month_end' AND period_end LIKE ?
                   ORDER BY period_end DESC LIMIT 1""",
                (a["account_id"], f"{prior_key}%"),
            ).fetchone()
            native_value = row["final_value"] if row else None
        else:
            row = conn.execute(
                """SELECT sum(market_value_base) mv FROM position_snapshot
                   WHERE account_id=? AND as_of_date LIKE ?""",
                (a["account_id"], f"{prior_key}%"),
            ).fetchone()
            native_value = row["mv"] if row and row["mv"] is not None else None

        if native_value is None:
            out[a["account_id"]] = None
            continue

        weight = 1.0
        if owner:
            w = ownership_weight(ownership, a["account_id"], owner)
            if w is None:
                out[a["account_id"]] = None
                continue
            weight = w

        rate = get_fx_rate(conn, a["base_currency"])
        out[a["account_id"]] = (native_value * rate * weight) if rate else None

    return out


def yearly_rollup(rows, amount_key="amount_sgd", date_key=None, year_key=None):
    """Re-aggregates already-fetched rows by year - deliberately NOT a new
    query. Reusing the exact rows a page already fetched (from
    get_transactions/get_fees/get_income) guarantees the yearly total can
    never disagree with the detail table below it, since both come from
    the same data, not a second query that could drift out of sync.
    Pass either date_key (raw transaction rows, year extracted from the
    date string) or year_key (already-grouped fees/income rows)."""
    from collections import defaultdict
    totals = defaultdict(lambda: {"amount_sgd": 0.0, "n": 0})
    for r in rows:
        if year_key:
            year = r.get(year_key) or "Unknown"
        else:
            year = (r[date_key][:4] if r.get(date_key) else "Unknown")
        totals[year]["amount_sgd"] += r[amount_key] or 0
        totals[year]["n"] += 1
    return [{"year": y, **v} for y, v in sorted(totals.items())]


def list_institutions(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT institution_id FROM account ORDER BY institution_id")]


def get_last_loaded_by_institution(conn):
    rows = conn.execute(
        """SELECT a.institution_id, max(p.as_of_date) AS last_position_date,
                  max(p.loaded_at) AS last_loaded_at
           FROM position_snapshot p JOIN account a ON a.account_id = p.account_id
           GROUP BY a.institution_id ORDER BY a.institution_id"""
    ).fetchall()
    return [dict(r) for r in rows]
