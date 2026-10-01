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

**Endowus** (2 accounts: Joint, Single) - no structured export exists on
this platform, only a scanned monthly PDF statement (no text layer). Handled
entirely by `ingest_endowus_pdf.py`, which OCRs each page (tesseract, 300 DPI)
and extracts positions, fees, deposits, and trades from it.
```bash
python3 ingest_endowus_pdf.py /path/to/statement.pdf --load
```
Note: unlike every other institution's loader, this one needs the target
account specified explicitly (`account_native_code=` in code - there's no
auto-detection from the file's own content yet, unlike CDP) since "Joint"
vs "Single" isn't otherwise distinguishable from the PDF alone without
building that detection.

The Single account is funded by CPF and SRS, and - confirmed by actually
loading its statement - **mixes funding sources within one account at the
goal level** (some goals SRS, some CPF OA). This matters for more than
just a label: CPF/SRS-funded goals bypass Endowus's own cash balance
entirely (confirmed: the Single account's Cash Balance section showed zero
activity in a month with $68,000 of real investment buys), and Endowus
charges its advisory fee differently there too - by selling a small unit
fraction directly, not deducting from cash.

Because of this, reconciliation runs **three independent gates**, not one:
1. Cash-balance replay (meaningless on its own for CPF/SRS goals - would
   pass trivially at 0=0 without validating any real activity for them).
2. Per-goal identity (Starting + Buy - Sell + Gain/loss = Ending) - the
   gate that actually matters for CPF/SRS goals, verified to hold exactly
   for both accounts, so it runs universally now, not conditionally.
3. Allocation-table-vs-overview-table cross-check - added after actually
   hitting the bug it exists to catch: an OCR layout variant (newly-opened,
   single-holding goal pages split the allocation row across lines) made
   two entire goals (20% of the Single account, $60,340) vanish from
   positions silently, while gates 1 and 2 both still reported success,
   since neither one touches the allocation table at all - they validate
   the overview table's own arithmetic, which was captured correctly.
   Worth internalizing as a general lesson: a reconciliation gate only
   validates what it actually touches - it says nothing about a different
   table that happens not to be checked, even if that's what gets loaded.

`position_snapshot.funding_source` (not `account.funding_source`, which
stays a coarser default for institutions that don't need this) carries the
real per-position value (SGD Cash / CPF OA / CPF SA / SRS).

A Modified Dietz return for that period is computed automatically from the
statement's own starting/ending balance and the real dated flows - see
`load_endowus_statement()` for why fees are deliberately excluded from that
number (folding them in would push the result the wrong direction, since
fees are paid from a separate cash bucket that doesn't touch the fund
NAV the return is based on). Not yet extended to account for CPF/SRS goals'
different fee mechanism - the Modified Dietz calc still assumes the
Joint-account cash-deduction model.

**CDP** (2 accounts: "CDP 0388", "CDP 9563") - a real text-layer PDF (no OCR needed,
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

**IBKR** (2 accounts: `U20780831` individual, `U21032806` joint - both in
one PDF) - a real text-layer PDF like CDP. Handled by `ingest_ibkr_pdf.py`.
Notable things specific to this one:
- IBKR gives its own TWR per account directly (like UBS), and a "Change in
  NAV" walk (Starting + Mark-to-Market + Deposits & Withdrawals + Position
  Transfers + Interest + Commissions + Sales Tax + FX = Ending) that's the
  reconciliation gate here.
- Internal transfers between the two accounts name the destination account
  number directly in the text - no fuzzy amount/date matching needed,
  unlike UBS's internal-transfer detection.
- The "Deposits & Withdrawals" section sits in a two-column page layout
  next to "Interest Accruals", and naive text extraction interleaves the
  two - a raw currency header can end up merged onto an unrelated line.
  Fixed by cross-referencing each entry's amount against the Cash Report
  (which IS single-column) rather than trusting the interleaved layout
  directly - worth remembering if a future statement's layout shifts again.
- The NAV table's "Interest Accruals" component is a real, if small, part
  of NAV that isn't in Open Positions or Forex Balances - loaded as its
  own cash-like position row. Missing this once caused both accounts to be
  short by exactly that amount despite the reconciliation gate passing -
  a reminder that the gate validates the *source's* arithmetic, not that
  every component of it actually got written to the database.
```bash
python3 ingest_ibkr_pdf.py /path/to/statement.pdf --load
```

**DBS** (4 accounts: Multiplier (SS), CPFIS-OA (SS), SRS (SS), POSB eSavings
(joint)) - a real text-layer "Consolidated Statement" PDF bundling several
very different things in one document. Handled by `ingest_dbs_pdf.py`.
```bash
python3 ingest_dbs_pdf.py /path/to/statement.pdf --load
```
Scope is deliberately narrower than what the statement actually contains -
agreed before any code was written, not discovered after:
- Multiplier/POSB eSavings: full parse, cash positions + complete
  transaction history, same pattern as every other cash ledger here.
- CPFIS-OA/SRS: **positions only** this phase (no transaction history -
  the per-transaction DBS fees for moving money to a broker are deferred).
  Within positions, a deliberate subset: cash balance and direct holdings
  are included, but money placed with UOB Kay Hian is EXCLUDED - it's the
  original cost basis for money also tracked, at its current and more
  accurate market value, by Endowus Single (confirmed: both statements
  reference the same UOB Kay Hian account number). Loading both would
  double-count the same capital at two different points in time.
- Mortgage loan: fully excluded, no liability tracking anywhere in this
  project. The payment itself still shows as an ordinary cash outflow in
  the Multiplier Account's own ledger - nothing special needed there.

Reconciliation runs two gates: cash-block replay (per account/currency),
and - importantly - the CPF/SRS section parses and validates EVERY line
(including the excluded ones) against each subsection's own stated
"Total:" line, which checks extraction correctness independent of the
inclusion/exclusion decision. This gate caught four real bugs during
development, including one near-identical in spirit to the Endowus Single
bug: a holdings table spanning a page break, where the continuation line
contained the true section header as a substring and silently reset
parser state, dropping a whole holding (Singapore Life, $116,000) with no
error. Worth treating as a standing lesson, not a one-off: any time a
table might continue across a page break, check whether its continuation
marker could collide with a real section-boundary check elsewhere in the
parser.

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

## Skip-if-already-loaded, and --force

Every ingester (`ingest_positions.py`, `ingest_transactions.py`,
`ingest_endowus_pdf.py`, `ingest_cdp_pdf.py`, `ingest_ibkr_pdf.py`) checks
whether a file with this exact filename has already been loaded (via
`db.is_source_file_loaded()`) and skips - printing a clear message - unless
`--force` is passed. For Endowus this also skips the slow OCR step
entirely, not just the database write. `run_monthly_load.py --force`
passes through to both the positions and transactions loaders.

This means re-running last month's command by accident is now safe. It
does NOT mean position loading was always safe to repeat - see the next
section for why that used to double your numbers.

## A real bug we hit for real: position loading was not idempotent

Loading the same position snapshot twice used to silently double every
account's value - nothing cleared the old snapshot before inserting the
new one, unlike `txn` (which dedupes via a unique key). This actually
happened during setup on a different machine and produced exactly-2x
totals across all 4 UBS accounts, which is how it was found.

Fixed in all four position-writing loaders (`ingest_positions.py`,
`ingest_endowus_pdf.py`, `ingest_cdp_pdf.py`, `ingest_ibkr_pdf.py`): each
now deletes any existing `position_snapshot` rows for the
`(account_id, as_of_date)` it's about to write, before inserting. Verified
for all four by deliberately reloading with `--force` and confirming row
counts and totals matched pre-reload exactly, not doubled - this was
caught live for IBKR specifically (140,897.98 -> 281,795.96 before the fix,
confirming the bug was real, not hypothetical).

**Takeaway for any future loader**: a reconciliation gate validates the
*source's* arithmetic (e.g. IBKR's NAV walk, Endowus's cash balance) - it
says nothing about whether re-running the same load duplicates what's
already in the database. Both protections are needed, separately.

## Web app (Phase 1: Flask, server-rendered)

`app.py` + `reports.py` + `templates/`. Run with `python3 app.py`, open
`http://localhost:5000`. Refresh after each reload - no rebuild step.

Design choice made deliberately for a clean Phase 2: `reports.py`'s
functions return plain dicts, not HTML - `app.py`'s routes render
templates around them. When Phase 2 adds an interactive JS layer, it can
call the same functions and `jsonify()` the result instead, with no
duplicated query logic.

**Built so far:** Overview page only (value by account, asset allocation,
last-loaded date per institution). The other 6 nav items (Positions,
Transactions, Fees, Income, Performance, Data Health) are wired into the
nav bar as placeholder pages, not yet built.

**Ownership toggle**: Consolidated / SS / SV, via `?owner=` query param.
Backed by `config/account_ownership.json` - **explicit config only, never
inferred** from a statement's own labels (UBS's "AND/OR" wording and
Endowus's account name "Joint" are NOT ownership evidence, per an explicit
design decision). An account missing from that file is excluded from an
owner's view (not zero-filled, not defaulted) and surfaced as a visible
warning banner - check `reports.get_account_values()` /
`get_asset_allocation()` if extending this pattern to a new page.

**Display identifiers** (`account.display_code` in the schema, populated
from `config/accounts.csv`): the UI shows real institution identifiers
(e.g. `546-337340-01`) while the internal `account_id` stays `UBS-0001`
under the hood - deliberately NOT renamed, to avoid touching the
already-reconciled pipeline. Full rename to real identifiers as the
internal ID is a deferred decision (see to-do list) - Option A (display
only) is what's built; Option B (rename the actual ID) is not.

**Funding source** (`account.funding_source`, e.g. `CASH`): account-level
default for institutions that don't need finer grain. For Endowus Single,
which genuinely mixes CPF/SRS/cash within one account, the real per-position
value lives in `position_snapshot.funding_source` instead - see the Endowus
section above.

## Current status snapshot

5 institutions, 14 accounts, all reconciled: UBS (4), Endowus (2: Joint and
Single - the latter is CPF/SRS-funded, mixing funding sources at the goal
level), CDP (2: 0388 and 9563), IBKR (2), DBS (4: Multiplier, CPFIS-OA,
SRS, POSB eSavings). Total ~5,686,293 SGD as of the last full load (July
2026 statements, except CDP which also has a December 2025 statement
loaded). Review queue empty across all institutions.

This completes the original 6-institution scope except CPF itself as a
standalone source - DBS's CPFIS-OA/SRS sections cover the CPF/SRS data
that's actually in scope for now (see DBS section above for the deliberate
exclusions and why).

Endowus's reconciliation now runs THREE independent gates (cash-balance
replay, per-goal identity, allocation-vs-overview cross-check) rather than
one - see the Endowus section above for why the first gate alone is
meaningless for CPF/SRS-funded goals, and why the third gate exists at all
(it's the one that would have caught two entire goals silently vanishing
from a real load - see git history on the "Add Endowus Single" commit for
the full story). DBS's second gate (CPF/SRS subsection-total validation)
is built on the same principle and caught a near-identical bug there too.

Git repo `PersPortfolio` pushed to `github.com/ss-ghub1/PersPortfolio`,
`main` branch. When syncing changes from this project to your local clone,
copy only the changed files (preserving paths) rather than re-unzipping
the whole project - a fresh `.git` folder has no remote configured, and
overwriting your existing `.git` loses it, requiring `git remote add
origin ...` again.

## Known hardcoded assumptions

A deliberate audit, not a list of bugs - some of these are fine as-is and
just worth knowing about; a couple are real risks worth remembering when
you hit them. Ordered by how much they could actually bite you.

1. **`load_endowus_statement()` defaults to `account_native_code="Endowus
   Joint"`.** This is the one with real silent-corruption risk: unlike
   CDP, DBS, and IBKR (all of which resolve which account a statement
   belongs to from the statement's own printed content), Endowus has no
   such auto-detection. If you ever ran the plain CLI
   (`python ingest_endowus_pdf.py <file> --load`) against the *Single*
   statement without explicitly overriding the account, it would NOT
   error - it would silently load Single's data under the Joint account.
   The reconciliation gates wouldn't catch this either, since they only
   validate a statement's own internal math, never "does this content
   actually belong to the account it's being attributed to." This is
   exactly why loading Single needs the longer `python -c "..."`
   one-liner with an explicit `account_native_code=` instead of the plain
   CLI - see the monthly workflow commands. Tracked as to-do item 7
   (Endowus needs the same auto-detection CDP already has).

2. **DBS's UOB-Kay-Hian/Navigator exclusion is matched by hardcoded name
   strings**, not a general rule: `EXCLUDE_NAMES = ("FUND MGT - UOB KAY
   HIAN", "FUND MGT - NAVIGATOR", "UOB KAY HIAN PTE LTD")` in
   `ingest_dbs_pdf.py`. This correctly encodes a real, current business
   decision (excluding money double-tracked via Endowus Single during the
   Dollardex migration - see the DBS section above), but it's literal-text
   matching. If DBS ever rewords that line item, or a new broker placement
   appears, this exclusion silently stops applying - no error, since this
   isn't something a reconciliation gate would catch (the gate validates
   that parsing is *complete*, not that the inclusion/exclusion *decision*
   is still correct). Related to to-do item 9 (watch DBS's CPFIS/SRS
   totals as the Dollardex migration completes) - if that item's check
   ever looks wrong, this is the first place to look.

3. **Endowus's funding-source vocabulary is a fixed, known list**:
   `FUNDING_SOURCE = r"(SGD Cash|CPF OA|CPF SA|SRS)"`. Safer failure mode
   than #2 - an unrecognized funding source would fail to match that row
   entirely (loud, via the reconciliation gates) rather than silently
   mis-tagging it. Would need a code change, not just a config edit, if a
   statement ever introduces a funding source we haven't seen (e.g. a
   different CPF sub-account).

4. **SGD is hardcoded as the reporting currency** in a few places
   (`export_excel.py`'s FX lookups specifically). Fine as long as SGD
   stays the reporting currency; would need actual code changes, not
   config, if that ever changed.

## Not built yet (next steps, roughly in order)

1. **Directory-based ingestion wrapper** (agreed design, not yet built):
   point at an input folder; it detects which parser each file needs by
   *content signature*, not filename (sniff xlsl/csv headers the way
   `ingest_transactions.py` already does internally for its own tab
   dispatch; sniff PDF text for institution-distinctive strings - "UBS AG"
   + "Portfolio number" for UBS, "InteractiveBrokers" + "Activity
   Statement" for IBKR, "Central Depository" for CDP, Endowus's own
   branding text after OCR for Endowus); determines the covered month from
   the statement's own content (already true for every PDF loader); skips
   files already loaded (now trivial - reuses `is_source_file_loaded()`
   directly). Flagged risk to keep in mind when building this: don't
   detect Endowus by "OCR found no text layer" - that shortcut breaks the
   moment a second scanned-PDF institution exists. Use explicit branding-
   text matching for every institution, including Endowus, from the start.
2. **Web app: Positions page.**
3. **Web app: Transactions page** (filterable by type and date range).
4. **Web app: Fees, Income, Performance, Data Health pages.**
5. `ingest_transactions.py`: currently only warns (doesn't fail loudly) on
   a sheet that doesn't match any known tab format - a wrong file can
   silently "succeed" with 0 rows loaded rather than erroring. Fix to fail
   loudly instead.
6. Modified Dietz return calc (`load_endowus_statement()`) still assumes
   the Joint account's cash-deduction fee mechanism - not yet updated for
   CPF/SRS goals' different (unit-sale) fee mechanism.
7. Endowus lacks auto-detection of which account (Joint vs Single) a
   statement belongs to - unlike CDP, which resolves this from the
   statement's own printed account number. Currently requires specifying
   `account_native_code=` explicitly in code.
8. DBS CPFIS-OA/SRS transaction-level detail not loaded (deferred by
   explicit agreement, not an oversight): the per-transaction fees DBS
   charges for moving money to a broker (TRANSACTION FEE, GST on PLACE/
   WITHDRAW FUND MGT transfers) are real costs, just not captured yet,
   since the transaction history for those sections is skipped entirely
   this phase.
9. Watch DBS's CPFIS-OA/SRS totals once Dollardex money finishes migrating
   to Endowus (in progress as of July 2026 - the "Navigator" placement was
   already down to $0.02): DBS's total should stay roughly flat as money
   moves between brokers, while Endowus's CPF/SRS positions grow to
   reflect it. If DBS's total doesn't track this as expected, the
   exclusion logic in `ingest_dbs_pdf.py` (currently keyed on matching
   "UOB KAY HIAN"/"NAVIGATOR" by name) may need revisiting.
10. Overview page polish: sort accounts by institution then account number
   (currently alphabetical by internal ID, which doesn't order UBS 1-4
   correctly); Endowus Joint's display label shouldn't show the email
   address.
11. Asset class labels differ by institution (UBS: "Equities - Equity
   investments", CDP: "Equities", Endowus: "Equity Fund") - not normalized
   into one taxonomy; shows as separate rows in asset allocation today.
12. CDP's December statements include an annual tax-summary section ("Other
   Dividends / Coupon / Capital Repayment / Redemption / Cash Distributions
   for the Period 1 Jan-31 Dec") covering the full calendar year, not just
   December. Never parse this as a transaction source (every event in it
   is also in that month's own Cash Transaction section, so loading both
   would double-count income) - but once a full calendar year of monthly
   CDP statements is loaded, sum whatever income was recorded that year
   and compare against this section's stated total as a pure validation
   check (never writes a transaction, so it can't double-count anything).
   Not useful until enough months exist to check against.
13. `performance.py`: the Endowus period return is computed and stored but
    not yet printed in the CLI report; the report header text still says
    "UBS's own statement TWR" for every account regardless of institution.
14. Positions page: "On Loan" is its own column but only ever populated for
    CDP holdings - wasted space for every other institution's rows.
    Consider showing it as a second, indented sub-row under the position
    instead (e.g. "  -> 2,000 on loan"), freeing up that column entirely.
15. **Deferred, larger decisions** (do once the basic web app design is
    proven out, not before):
    - Rename internal account IDs to real institution identifiers
      (Option B) - touches `accounts.csv`, `account_alias.csv`,
      `confirmed_transfers.csv`, needs full re-reconciliation after.
    - Interactive dashboard (Phase 2): JS charting layer on the same
      Flask backend, additive per the `reports.py` design above, not a
      rewrite.
    - Upgrading `db.py` to SQLAlchemy for a Postgres option - schema is
      already portable SQL, low-cost whenever it's wanted.
