"""
PersPortfolio web app.

Usage:
    python3 app.py
Then open http://localhost:5000 in a browser. Refresh after each monthly
data reload (run_monthly_load.py etc.) to see updated numbers - no separate
build/restart step needed for data changes, just re-run the ingestion
scripts against data/portfolio.db and reload the page.
"""
from flask import Flask, render_template, request

from db import get_connection, load_ownership
import reports

app = Flask(__name__)

NAV_PAGES = [
    ("overview", "Overview"),
    ("positions", "Positions"),
    ("transactions", "Transactions"),
    ("fees", "Fees"),
    ("income", "Income"),
    ("performance", "Performance"),
    ("data_health", "Data Health"),
]


def _owner_from_request():
    """None = consolidated. Validated against actual owners in config -
    an unrecognized ?owner= value falls back to consolidated rather than
    silently filtering to nothing."""
    owner = request.args.get("owner")
    if not owner:
        return None
    owners = reports.list_owners(load_ownership())
    return owner if owner in owners else None


@app.context_processor
def inject_nav():
    return {"nav_pages": NAV_PAGES, "owners": reports.list_owners(load_ownership()),
            "current_owner": _owner_from_request()}


@app.route("/")
def index():
    return overview()


@app.route("/overview")
def overview():
    owner = _owner_from_request()
    conn = get_connection()
    accounts = reports.get_account_values(conn, owner)
    allocation = reports.get_asset_allocation(conn, owner)
    last_loaded = reports.get_last_loaded_by_institution(conn)
    conn.close()
    return render_template(
        "overview.html", active_page="overview", owner=owner,
        accounts=accounts, allocation=allocation, last_loaded=last_loaded,
    )


@app.route("/positions")
def positions():
    owner = _owner_from_request()
    institution = request.args.get("institution") or None
    conn = get_connection()
    institutions = reports.list_institutions(conn)
    if institution and institution not in institutions:
        institution = None
    data = reports.get_positions(conn, owner, institution_filter=institution)
    conn.close()
    return render_template(
        "positions.html", active_page="positions", owner=owner,
        data=data, institutions=institutions, current_institution=institution,
    )


@app.route("/transactions")
def transactions():
    owner = _owner_from_request()
    institution = request.args.get("institution") or None
    txn_type = request.args.get("txn_type") or None
    date_from = request.args.get("date_from") or None
    date_to = request.args.get("date_to") or None
    conn = get_connection()
    institutions = reports.list_institutions(conn)
    txn_types = reports.list_txn_types(conn)
    if institution and institution not in institutions:
        institution = None
    if txn_type and txn_type not in txn_types:
        txn_type = None
    data = reports.get_transactions(conn, owner, institution_filter=institution,
                                     txn_type_filter=txn_type, date_from=date_from, date_to=date_to)
    conn.close()
    return render_template(
        "transactions.html", active_page="transactions", owner=owner,
        data=data, institutions=institutions, current_institution=institution,
        txn_types=txn_types, current_txn_type=txn_type,
        date_from=date_from or "", date_to=date_to or "",
    )


@app.route("/fees")
def fees():
    owner = _owner_from_request()
    conn = get_connection()
    data = reports.get_fees(conn, owner)
    detail = reports.get_transactions(conn, owner, txn_type_filter="FEE")
    conn.close()
    return render_template("fees.html", active_page="fees", owner=owner, data=data, detail=detail)


@app.route("/income")
def income():
    owner = _owner_from_request()
    conn = get_connection()
    data = reports.get_income(conn, owner)
    detail = reports.get_transactions(conn, owner, txn_type_filter="INCOME")
    conn.close()
    return render_template("income.html", active_page="income", owner=owner, data=data, detail=detail)


STUB_PAGES = [p for p in NAV_PAGES[1:] if p[0] not in ("positions", "transactions", "fees", "income")]
for slug, label in STUB_PAGES:
    def _make_stub(slug=slug, label=label):
        def _stub():
            return render_template("coming_soon.html", active_page=slug, page_label=label,
                                    owner=_owner_from_request())
        return _stub
    app.add_url_rule(f"/{slug}", endpoint=slug, view_func=_make_stub())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
