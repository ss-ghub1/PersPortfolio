"""
Database connection helper: opens the SQLite file, applies the schema,
and loads the reference config tables (accounts, account aliases,
classification rules) from config/*.csv.

Usage:
    from db import get_connection
    conn = get_connection()
"""
import csv
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "portfolio.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
CONFIG_DIR = BASE_DIR / "config"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_schema(conn)
    _load_reference_config(conn)
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.commit()


def _load_reference_config(conn: sqlite3.Connection) -> None:
    """(Re)loads institution / account / alias / classification-rule tables
    from the editable CSVs in config/. Safe to re-run: uses INSERT OR REPLACE
    for accounts/aliases, and fully refreshes classification_rule each time
    so rule edits take effect immediately."""

    institutions = set()
    accounts_csv = CONFIG_DIR / "accounts.csv"
    with open(accounts_csv, newline="") as f:
        for row in csv.DictReader(f):
            institutions.add(row["institution_id"])
    for inst in institutions:
        conn.execute(
            "INSERT OR IGNORE INTO institution (institution_id, name) VALUES (?, ?)",
            (inst, inst),
        )

    with open(accounts_csv, newline="") as f:
        for row in csv.DictReader(f):
            account_id = _clean_account_id(row["institution_id"], row["native_account_code"])
            conn.execute(
                """INSERT INTO account
                     (account_id, institution_id, banking_relationship,
                      native_account_code, account_label, base_currency,
                      display_code, funding_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(institution_id, native_account_code) DO UPDATE SET
                     account_label = excluded.account_label,
                     base_currency = excluded.base_currency,
                     banking_relationship = excluded.banking_relationship,
                     display_code = excluded.display_code,
                     funding_source = excluded.funding_source
                """,
                (
                    account_id,
                    row["institution_id"],
                    row.get("banking_relationship"),
                    row["native_account_code"],
                    row.get("account_label"),
                    row.get("base_currency"),
                    row.get("display_code"),
                    row.get("funding_source") or "CASH",
                ),
            )

    alias_csv = CONFIG_DIR / "account_alias.csv"
    if alias_csv.exists():
        with open(alias_csv, newline="") as f:
            for row in csv.DictReader(f):
                account_id = _clean_account_id(row["institution_id"], row["native_account_code"])
                conn.execute(
                    """INSERT INTO account_alias
                         (institution_id, raw_identifier, alias_type, currency, account_id)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(institution_id, raw_identifier, alias_type) DO UPDATE SET
                         account_id = excluded.account_id, currency = excluded.currency
                    """,
                    (row["institution_id"], row["raw_identifier"], row["alias_type"],
                     row.get("currency"), account_id),
                )

    rules_csv = CONFIG_DIR / "classification_rules.csv"
    if rules_csv.exists():
        conn.execute("DELETE FROM classification_rule")
        with open(rules_csv, newline="") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    """INSERT INTO classification_rule
                         (priority, match_field, match_pattern, txn_type, txn_subtype)
                       VALUES (?, ?, ?, ?, ?)""",
                    (int(row["priority"]), row["match_field"], row["match_pattern"],
                     row["txn_type"], row.get("txn_subtype") or None),
                )

    conn.commit()


def _clean_account_id(institution_id: str, native_account_code: str) -> str:
    """Turns '0546 00337340 0001' -> 'UBS-0001' style id. Falls back to a
    slugified version of the native code for formats we haven't seen yet."""
    suffix = native_account_code.strip().split(" ")[-1]
    return f"{institution_id}-{suffix}"


def load_ownership() -> dict:
    """Returns {account_id: [{"owner": "SS", "pct": 50}, ...]}. Explicit
    config only - an account missing from account_ownership.json should be
    treated as ownership-unknown by callers, never defaulted."""
    import json
    path = CONFIG_DIR / "account_ownership.json"
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def is_source_file_loaded(conn: sqlite3.Connection, table: str, source_file: str) -> bool:
    """True if any row in `table` already has this source_file. Used by every
    ingester to skip a file that's already been loaded, rather than silently
    re-processing (and for position-writing loaders, re-inserting) it. table
    must be 'position_snapshot' or 'txn' - never build this string from
    untrusted input."""
    assert table in ("position_snapshot", "txn"), f"unexpected table: {table}"
    row = conn.execute(f"SELECT 1 FROM {table} WHERE source_file=? LIMIT 1", (source_file,)).fetchone()
    return row is not None


def resolve_account_id(conn: sqlite3.Connection, institution_id: str, raw_identifier: str,
                        alias_type: str = None) -> str | None:
    """Looks up account_id for a raw identifier seen in a cash-ledger tab
    (alias) or directly as a native_account_code (position/security file)."""
    row = conn.execute(
        "SELECT account_id FROM account WHERE institution_id=? AND native_account_code=?",
        (institution_id, raw_identifier),
    ).fetchone()
    if row:
        return row["account_id"]

    q = "SELECT account_id FROM account_alias WHERE institution_id=? AND raw_identifier=?"
    params = [institution_id, raw_identifier]
    if alias_type:
        q += " AND alias_type=?"
        params.append(alias_type)
    row = conn.execute(q, params).fetchone()
    return row["account_id"] if row else None
