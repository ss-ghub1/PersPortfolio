# Portfolio & Transaction Tracker

A local, license-free SQLite + Python data model for tracking investment
portfolios across institutions, built from whatever extracts you can pull
on login: a periodic **position snapshot** and a **life-to-date transaction
extract**. No manual file bookkeeping - each month you run one script.

## Why this design

You can't get month-end position files, only a live snapshot whenever you
log in, but you *can* pull the full transaction history any time. So the
model treats snapshots as **checkpoints for validation**, and derives
everything else - historical positions, income, fees, performance - by
replaying transactions. Every load runs a reconciliation check (`reconcile.py`)
that compares its own transaction-replayed cash balance against the bank's
own running balance and against the latest snapshot, so a bad or incomplete
extract is caught immediately instead of silently corrupting your numbers.

## Folder structure

```
portfolio_tracker/
  schema.sql                     # table definitions (see this for full field list)
  db.py                          # connection + loads config/*.csv into the db
  classify.py                    # rule-based txn_type/txn_subtype classifier
  ingest_positions.py            # parses a UBS-style position snapshot workbook
  ingest_transactions.py         # parses security-leg + per-currency cash-leg tabs (xlsx or csv)
  ingest_statement_pdf.py        # parses UBS statement PDFs' own monthly/annual TWR tables
  ingest_endowus_pdf.py          # parses Endowus statement PDFs (scanned/OCR-based)
  postprocess.py                 # detects transfers between your own accounts
  reconcile.py                   # the validation checks described above
  performance.py                 # statement-based (authoritative) + transaction-derived performance
  demo_reports.py                # example queries: value by account, fees, income
  export_excel.py                # snapshot workbook: overview, positions, fees, income, performance
  run_monthly_load.py            # single entry point tying it all together (UBS side)
  config/
    accounts.csv                 # your accounts across all institutions - edit account_label freely
    account_alias.csv            # maps raw cash-ledger account numbers -> account
    classification_rules.csv     # raw bank label -> txn_type/txn_subtype (data, not code)
    confirmed_transfers.csv      # user-confirmed transfer destinations automatic pairing couldn't resolve
  data/
    portfolio.db                 # the SQLite database (created on first run)
```

## Monthly workflow

```bash
python3 run_monthly_load.py \
  --positions  /path/to/new_position_snapshot.xlsx \
  --transactions /path/to/new_transaction_extract.xlsx
```

Either argument can be omitted if you only have one of the two this month.
Re-running with an overlapping life-to-date transaction file is safe - the
`txn` table's unique key means rows already loaded are silently skipped, not
duplicated. Then:

```bash
python3 demo_reports.py    # portfolio value, asset allocation, fees, income
```

or query `data/portfolio.db` directly with any SQLite client / pandas.

Each month also: load any new UBS statement PDFs (`ingest_statement_pdf.py
<pdf> --load`) and the new Endowus statement (`ingest_endowus_pdf.py <pdf>
--load`), then refresh the Excel snapshot with `python3 export_excel.py`.

## The data model (see schema.sql for full detail)

- **institution** - one row per bank/platform (UBS and Endowus today, 2 more planned)
- **account** - a portfolio/custody account (4 UBS portfolios + 1 Endowus account today); has a base currency
- **account_alias** - maps raw identifiers seen in extracts (e.g. a cash
  sub-account number that doesn't itself say which portfolio it belongs to)
  to the clean `account_id`. This is the piece you maintain by hand per
  institution, since it's mostly static.
- **instrument** - one row per security (keyed on ISIN, falls back to Valor)
- **position_snapshot** - append-only; one load per login. Cash is a row
  here (`is_cash=1`), not a separate table, differentiated by currency.
- **txn** - both transaction legs land here, tagged by `source_leg`:
  `SECURITY` (from the UBS security-side extract - has quantity, price,
  realized P/L) and `CASH` (from the per-currency ledgers - has the bank's
  own running balance, which is what powers reconciliation).
- **classification_rule** - a data table, not code, mapping raw bank labels
  to `txn_type` (BUY/SELL/DIVIDEND/FEE/TRANSFER/DEPOSIT/WITHDRAWAL/...) and
  a finer `txn_subtype` (e.g. `ADVISORY_FEE` vs `ADR_HANDLING_FEE`). Add
  rows here as new labels show up in future extracts - no code change needed.
- **reconciliation_log** - every check run is logged for audit.

## Current state (as of the last full load)

- **5 accounts** across 2 institutions: UBS portfolios 1-4, and Endowus Joint.
  (Portfolio 4's data comes via a proxy export - UBS offers no Excel download
  for it, so a broader direct-equity export is filtered by hand; UBS labels
  it `R800` in the raw files, and `config/account_alias.csv` maps that to
  `UBS-0004`.)
- **Total value: ~3.27M SGD** (`demo_reports.py`), with fees and income
  reported across both institutions and the review queue empty.
- UBS cash-balance replay matches UBS's own running balance on every ledger.
  Two known, explained gaps remain in `reconcile.py`'s snapshot-vs-ledger
  check (a 10-day extract lag on one account, and a transaction extract for
  Portfolio 1's USD cash sub-account that stops at 2026-03-16) - these are
  source-data coverage gaps, not parsing errors.
- Transfers between your own accounts are auto-paired where both legs are
  visible; ones with no visible destination ledger are recorded in
  `config/confirmed_transfers.csv` once you've confirmed where they went
  (e.g. the June 2026 transfers that funded Portfolio 4). One transfer
  (2026-07-03, 219,941.57 SGD) is tagged `CONFIRMED_TRANSFER_TO_UNKNOWN` -
  internal in nature, destination unidentified.

## Performance

`performance.py` reports account performance two ways:

- **Statement-based (authoritative)**: if you've loaded a UBS statement PDF
  for an account (`ingest_statement_pdf.py`), this uses UBS's own linked TWR
  calculation, month by month, back to account inception. No assumptions
  needed - this is the number to trust.
- **Transaction-derived (Modified Dietz, BMV=0)**: a fallback for accounts
  with no statement PDF. This assumes the account started at zero value on
  its first *visible* transaction date, which breaks down badly if there's
  real history before your transaction extract's coverage window (this is
  exactly what happened when we first tried it on accounts 1 and 2 - the
  numbers came out nonsensical, which is what led to pulling statement PDFs
  in the first place). Only trust this once a statement PDF confirms it's
  in the right ballpark.

Loading a statement PDF:
```bash
python3 ingest_statement_pdf.py /path/to/statement.pdf --load
```
It maps the PDF's portfolio number to your `account_id` automatically and
extracts both the monthly (current year) and annual (since inception)
UBS-computed TWR tables into `account_valuation_history`. Column positions
are detected per-page from the header row, not hardcoded, so this should
hold up against future statements even if margins/currency shift things
slightly - but always spot-check a new statement's numbers against the
printed output once, the way we did here.



## Institutions currently wired up

**UBS** (4 accounts: portfolios 1-4) - structured Excel/CSV exports for
positions and transactions, plus PDF statements for authoritative TWR.
Handled by `ingest_positions.py` / `ingest_transactions.py` / `run_monthly_load.py`.

**Endowus** (1 account, all goals combined) - no structured export exists on
this platform, only a scanned monthly PDF statement (no text layer). Handled
entirely by `ingest_endowus_pdf.py`, which OCRs each page (tesseract, 300 DPI)
and extracts positions, fees, deposits, and trades from it. Because OCR is
inherently less trustworthy than a text-layer extract, this loader **refuses
to load a statement that doesn't reconcile**: it replays the statement's own
starting balance + every cash transaction and checks it lands on the stated
ending balance to the cent before touching the database. Run it with:
```bash
python3 ingest_endowus_pdf.py /path/to/statement.pdf --load
```
A Modified Dietz return for that period is computed automatically from the
statement's own starting/ending balance and the real dated flows - see
`load_endowus_statement()` for why fees are deliberately excluded from that
number (folding them in would push the result the wrong direction, since
fees are paid from a separate cash bucket that doesn't touch the fund
NAV the return is based on).

**CDP** (1 account, "CDP 0388") - a real text-layer PDF (no OCR needed,
unlike Endowus), but still no structured export for transactions (a
positions-only Excel exists on the portal but wasn't used, for consistency -
the PDF already covers both). Handled by `ingest_cdp_pdf.py`. Two things
specific to this depository, both confirmed against the statement's own
arithmetic rather than assumed:
- Securities on loan (Securities Borrowing & Lending) are NOT included in
  the statement's own Total Balance - they're added on top of the held
  quantity/value, tracked via `position_snapshot.quantity_on_loan`.
- Cash payouts go to an external bank (DBS - not one of our other tracked
  accounts), so they're tagged `WITHDRAWAL`, not `TRANSFER`. If a future
  institution pays out to an account we DO track, that should be a
  `TRANSFER` instead - check before assuming.
Same reconciliation-gate pattern as Endowus, checked against the statement's
own Main/Total Balance and cash brought-forward/carried-forward figures.
```bash
python3 ingest_cdp_pdf.py /path/to/statement.pdf --load
```

## Extending to a new institution

Two paths, depending on what that institution actually offers:
1. **Structured export exists (preferred)** - write an `ingest_positions_<x>.py`
   / `ingest_transactions_<x>.py` pair mapping that format onto the same
   schema fields, the way UBS's did. Reuse `classify.py`, `postprocess.py`,
   `reconcile.py`, `performance.py`, `demo_reports.py`, `export_excel.py`
   unchanged - they're all institution-agnostic already, and adding Endowus
   required zero changes to any of them.
2. **No structured export (scanned PDF only)** - follow Endowus's pattern:
   OCR the pages, and gate every load behind a reconciliation check against
   whatever the statement itself states as a period total. Don't relax that
   gate even under time pressure - it's the only thing standing between OCR
   misreads and silently wrong numbers in the database.

Either way: add the institution's account(s) to `config/accounts.csv` first.

## Not built yet (next steps)

- Institution #4 (your one remaining investment account - UBS, Endowus
  and CDP are done).
- Asset class labels differ by institution (UBS says "Equities - Equity
  investments", CDP says "Equities", Endowus says "Equity Fund" - all
  the same underlying category). Not yet normalized into one taxonomy;
  currently shows as separate rows in the asset allocation report.
- A front end / dashboard for browsing all of this visually (the Excel
  snapshot from `export_excel.py` is the interim option).
- Display polish in `performance.py`: the Endowus period return is stored
  but not yet printed in the report, and the report header still says
  "UBS's own statement TWR" for every account.
- Upgrading `db.py` to SQLAlchemy if/when you want a Postgres option - the
  schema is already portable SQL, so this is a low-cost future step.
