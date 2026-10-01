"""
Data-fetching functions for the web app. Deliberately separate from app.py:
these return plain dicts/lists, not HTML - app.py's routes call these and
render templates around the result. When Phase 2 adds JSON endpoints for
an interactive dashboard, they call the exact same functions and just
jsonify() the result instead of rendering a template - no logic duplicated.
"""
from db import get_connection, load_ownership


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

    rows.sort(key=lambda r: -(r["value_sgd"] or 0))
    return {"rows": rows, "total_sgd": total_sgd, "unknown_ownership_accounts": unknown}


def get_asset_allocation(conn, owner=None):
    """Same owner-weighting logic as get_account_values, aggregated by asset class."""
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

    totals, unknown = {}, set()
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
        totals[r["asset_class"]] = totals.get(r["asset_class"], 0.0) + val_sgd

    grand_total = sum(totals.values())
    out = [{"asset_class": k, "value_sgd": v, "pct": (v / grand_total * 100) if grand_total else 0}
           for k, v in sorted(totals.items(), key=lambda x: -x[1])]
    return {"rows": out, "total_sgd": grand_total, "unknown_ownership_accounts": sorted(unknown)}


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


def get_fees(conn, owner=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Grouped by account + subtype, matching demo_reports.py's shape."""
    return _grouped_cash_report(conn, "FEE", owner, group_cols=["t.txn_subtype"])


def get_income(conn, owner=None):
    """Returns {"rows": [...], "total_sgd": float, "unknown_ownership_accounts": [...]}.
    Grouped by account + year, matching demo_reports.py's shape."""
    return _grouped_cash_report(conn, "INCOME", owner, group_cols=["substr(t.trade_date,1,4)"],
                                 group_labels=["year"])


def _grouped_cash_report(conn, txn_type, owner, group_cols, group_labels=None):
    group_labels = group_labels or [c.split(".")[-1] for c in group_cols]
    ownership = load_ownership()
    query = f"""
        SELECT t.account_id, a.institution_id, a.display_code,
               {', '.join(f'{c} AS {l}' for c, l in zip(group_cols, group_labels))},
               t.currency, round(sum(t.gross_amount), 2) AS total, count(*) n
        FROM txn t JOIN account a ON a.account_id = t.account_id
        WHERE t.txn_type = '{txn_type}'
        {_leg_preferred_query(txn_type)}
        GROUP BY t.account_id, {', '.join(group_cols)}, t.currency
        ORDER BY a.institution_id, a.account_id
    """
    rows, total_sgd, unknown = [], 0.0, set()
    for r in conn.execute(query).fetchall():
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
