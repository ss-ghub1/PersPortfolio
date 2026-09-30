-- Portfolio & transaction data model
-- Institution-agnostic: designed to hold multiple institutions, each with
-- multiple accounts (portfolios), each account holding both cash (by currency)
-- and securities.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. INSTITUTION
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS institution (
    institution_id      TEXT PRIMARY KEY,      -- e.g. 'UBS'
    name                 TEXT NOT NULL,
    notes                TEXT
);

-- ---------------------------------------------------------------------------
-- 2. ACCOUNT  (= "portfolio" in UBS terms)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account (
    account_id           TEXT PRIMARY KEY,      -- clean internal id, e.g. 'UBS-0001'
    institution_id        TEXT NOT NULL REFERENCES institution(institution_id),
    banking_relationship  TEXT,                  -- UBS 'Banking relationship' number
    native_account_code   TEXT NOT NULL,         -- raw portfolio number from source, e.g. '0546 00337340 0001'
    account_label         TEXT,                  -- friendly name, user-editable
    base_currency         TEXT,                  -- 'Valued in' currency for this portfolio
    status                TEXT DEFAULT 'ACTIVE',
    UNIQUE(institution_id, native_account_code)
);

-- ---------------------------------------------------------------------------
-- 3. ACCOUNT ALIAS MAP
-- Cash-ledger extracts identify accounts by a currency sub-account number
-- (e.g. '0546 00337340.13') that does not by itself say which portfolio it
-- belongs to. This table is the (mostly static, user-maintained) mapping
-- from any raw identifier seen in any extract -> the clean account_id above.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_alias (
    alias_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_id         TEXT NOT NULL REFERENCES institution(institution_id),
    raw_identifier          TEXT NOT NULL,        -- e.g. '0546 00337340.13' or product code '0546 00337340.13J'
    alias_type              TEXT NOT NULL,        -- 'CASH_SUBACCOUNT' | 'PRODUCT_CODE' | 'PORTFOLIO_CODE'
    currency                TEXT,                 -- currency this alias represents, if a cash sub-account
    account_id              TEXT NOT NULL REFERENCES account(account_id),
    UNIQUE(institution_id, raw_identifier, alias_type)
);

-- ---------------------------------------------------------------------------
-- 4. INSTRUMENT
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instrument (
    instrument_id          TEXT PRIMARY KEY,      -- ISIN preferred; falls back to Valor if ISIN missing
    isin                    TEXT,
    valor                   TEXT,
    name                     TEXT,
    asset_class              TEXT,                 -- from 'Group of products': Equities, Bonds, Real estate, ...
    sub_asset_class           TEXT,                 -- from txn file 'Sub-asset class' where available
    instrument_currency        TEXT,                 -- native trading currency
    sector                     TEXT
);

-- ---------------------------------------------------------------------------
-- 5. POSITION SNAPSHOT  (append-only; one load per login/extract)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position_snapshot (
    snapshot_row_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id                TEXT NOT NULL REFERENCES account(account_id),
    instrument_id              TEXT REFERENCES instrument(instrument_id),  -- NULL for pure cash rows
    is_cash                     INTEGER NOT NULL DEFAULT 0,                  -- 1 = cash row, 0 = security row
    as_of_date                   TEXT NOT NULL,                               -- ISO date 'YYYY-MM-DD'
    position_currency              TEXT NOT NULL,                               -- 'Ccy.' column (native currency)
    quantity                        REAL,                                        -- units, or cash amount if is_cash
    quantity_on_loan                  REAL,                                        -- units out on securities lending, already included in `quantity`
    cost_price                       REAL,
    cost_value_native                 REAL,
    market_price                       REAL,
    market_value_native                 REAL,                                        -- in position_currency
    market_value_base                    REAL,                                        -- in account's base_currency ('Market value' col)
    unrealized_pl_pct                     REAL,
    accrued_interest                       REAL,
    pct_of_portfolio                        REAL,
    source_file                              TEXT NOT NULL,
    loaded_at                                 TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_account_date ON position_snapshot(account_id, as_of_date);
CREATE INDEX IF NOT EXISTS idx_snapshot_instrument ON position_snapshot(instrument_id);

-- ---------------------------------------------------------------------------
-- 6. "TRANSACTION"  (both security legs and cash legs land here)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS txn (
    txn_row_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id                 TEXT NOT NULL REFERENCES account(account_id),
    instrument_id                TEXT REFERENCES instrument(instrument_id),   -- NULL for pure cash movements
    source_leg                    TEXT NOT NULL,                                -- 'SECURITY' | 'CASH'
    trade_date                     TEXT NOT NULL,
    booking_date                     TEXT,
    value_date                        TEXT,
    txn_type                           TEXT NOT NULL,                            -- BUY, SELL, DIVIDEND, FEE, TRANSFER, FX_CONVERSION, DEPOSIT, WITHDRAWAL, REVERSAL, OTHER
    txn_subtype                         TEXT,                                     -- e.g. ADVISORY_FEE, ADR_HANDLING_FEE
    currency                              TEXT NOT NULL,
    quantity                               REAL,                                    -- units transacted (security leg)
    price                                    REAL,                                    -- transaction price (security leg)
    gross_amount                             REAL,                                    -- signed: +credit / -debit
    fee_amount                                REAL,
    tax_amount                                 REAL,
    running_balance                             REAL,                                   -- cash leg only, from source 'Balance' col
    fx_rate                                      REAL,
    realized_pl                                   REAL,
    raw_txn_type_label                             TEXT,                                  -- original UBS description, kept for audit
    raw_description                                 TEXT,
    source_reference                                 TEXT,                                  -- UBS Transaction no. / Order no.
    source_file                                       TEXT NOT NULL,
    loaded_at                                          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, source_leg, trade_date, source_reference, instrument_id, quantity,
           gross_amount, currency, raw_txn_type_label)
      -- dedup key: re-loading an overlapping life-to-date extract will not create duplicate rows.
      -- instrument_id/quantity/raw_txn_type_label are all needed because a single batch transfer
      -- reference can cover several securities, and even one security can have several distinct
      -- legs (e.g. 'transfer out' + 'title shift out') on the same date with no cash amount to
      -- otherwise distinguish them.
);

CREATE INDEX IF NOT EXISTS idx_txn_account_date ON txn(account_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_txn_type ON txn(txn_type, txn_subtype);
CREATE INDEX IF NOT EXISTS idx_txn_instrument ON txn(instrument_id);

-- ---------------------------------------------------------------------------
-- 7. FX RATE  (for cross-currency consolidation later)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fx_rate (
    rate_date          TEXT NOT NULL,
    from_ccy             TEXT NOT NULL,
    to_ccy                 TEXT NOT NULL,
    rate                     REAL NOT NULL,
    source_file               TEXT,
    PRIMARY KEY (rate_date, from_ccy, to_ccy)
);

-- ---------------------------------------------------------------------------
-- 8. CLASSIFICATION RULES  (drives txn_type / txn_subtype assignment)
-- Rules are matched in priority order (lowest number first) against a field.
-- Kept as a table, not code, so refining the taxonomy is a data change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS classification_rule (
    rule_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    priority            INTEGER NOT NULL,
    match_field           TEXT NOT NULL,     -- which raw field to match against
    match_pattern           TEXT NOT NULL,     -- substring match, case-insensitive
    txn_type                  TEXT NOT NULL,
    txn_subtype                 TEXT
);

-- ---------------------------------------------------------------------------
-- 10. ACCOUNT VALUATION HISTORY (from UBS statement PDFs' own TWR tables)
-- UBS computes proper linked TWR month by month in these statements, going
-- back to account inception. This is authoritative ground truth for
-- historical performance - far better than anything we could reconstruct
-- from partial transaction data, and it's what performance.py should prefer
-- wherever it's available.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_valuation_history (
    history_row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id             TEXT NOT NULL REFERENCES account(account_id),
    period_type              TEXT NOT NULL,   -- 'month_end' | 'year_end'
    period_end                 TEXT NOT NULL,   -- ISO date
    period_label                 TEXT,
    currency                       TEXT NOT NULL,
    final_value                     REAL,          -- net assets at period end
    inflows                          REAL,
    outflows                          REAL,
    gain_value                         REAL,          -- period's own performance $ value
    twr_pct                             REAL,          -- period's own TWR %
    cum_value                            REAL,          -- cumulative $ value since the statement's inception anchor
    cum_twr_pct                           REAL,          -- cumulative TWR % since inception anchor
    source_file                            TEXT NOT NULL,
    loaded_at                                TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, period_type, period_end, source_file)
);
CREATE INDEX IF NOT EXISTS idx_valhist_account ON account_valuation_history(account_id, period_type, period_end);

-- ---------------------------------------------------------------------------
-- 9. RECONCILIATION LOG  (results of each reconciliation run, for audit)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reconciliation_log (
    recon_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    account_id               TEXT NOT NULL REFERENCES account(account_id),
    scope                      TEXT NOT NULL,   -- e.g. 'CASH_BALANCE:USD'
    period_start                 TEXT,
    period_end                     TEXT,
    expected_value                   REAL,
    actual_value                       REAL,
    difference                           REAL,
    status                                 TEXT,   -- 'OK' | 'MISMATCH'
    notes                                    TEXT
);
