"""
Applies classification_rule rows (loaded from config/classification_rules.csv)
to a raw transaction label to get (txn_type, txn_subtype).

Rules are matched as case-insensitive substrings, in priority order
(lowest number first); the first match wins. Unmatched labels get
txn_type='OTHER' / subtype='UNCLASSIFIED' so nothing silently disappears -
these show up in the review query after each load.
"""
import sqlite3

_rules_cache = None


def _load_rules(conn: sqlite3.Connection):
    global _rules_cache
    if _rules_cache is None:
        rows = conn.execute(
            "SELECT match_field, match_pattern, txn_type, txn_subtype "
            "FROM classification_rule ORDER BY priority ASC"
        ).fetchall()
        _rules_cache = [(r["match_pattern"].lower(), r["txn_type"], r["txn_subtype"]) for r in rows]
    return _rules_cache


def reset_cache():
    global _rules_cache
    _rules_cache = None


def classify(conn: sqlite3.Connection, raw_label: str):
    """Returns (txn_type, txn_subtype) for a raw UBS label string."""
    if not raw_label:
        return "OTHER", "UNCLASSIFIED"
    label_lower = raw_label.lower()
    for pattern, txn_type, txn_subtype in _load_rules(conn):
        if pattern in label_lower:
            return txn_type, txn_subtype
    return "OTHER", "UNCLASSIFIED"
