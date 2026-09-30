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


for slug, label in NAV_PAGES[1:]:
    def _make_stub(slug=slug, label=label):
        def _stub():
            return render_template("coming_soon.html", active_page=slug, page_label=label,
                                    owner=_owner_from_request())
        return _stub
    app.add_url_rule(f"/{slug}", endpoint=slug, view_func=_make_stub())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
