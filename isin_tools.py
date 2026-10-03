"""ISIN helpers shared by the Endowus parser, the Endowus loader and the
instrument-ID repair tool (fix_instrument_ids.py).

Why this exists: statements read by OCR produce instrument IDs that look like
ISINs but are not - letter O or Q where the digit 0 belongs ('IEOOOXNHMJW8'
for the real 'IE000XNHMJW8'), or no ISIN at all, which falls back to a
'NAME:<fund name>' placeholder. The same fund then has one ID in OCR-era
months and another in text-layer months, fragmenting instrument-level
history. An ISIN's check digit says WHETHER an ID is garbled; look-alike
matching against IDs already known to be valid says what it should have been.
"""
import difflib
import re


def isin_valid(s):
    """ISIN shape AND check digit (Luhn over the letter-expanded digits).
    Shape alone cannot tell letter O from digit 0 - 'IEOOOXNHMJW8' has the same
    shape as 'IE000XNHMJW8'. Verified on 102 real ISINs from five independent
    sources: all pass; every OCR-garbled variant seen fails."""
    if not s or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", s):
        return False
    total = 0
    for i, ch in enumerate(reversed("".join(str(int(c, 36)) for c in s))):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


# Characters OCR confuses with one another (symmetric). Only O/Q-for-0 has been
# seen in practice; the rest are the standard look-alikes.
_PAIRS = {("O", "0"), ("Q", "0"), ("D", "0"), ("I", "1"), ("L", "1"),
          ("S", "5"), ("B", "8"), ("Z", "2")}
_PAIRS |= {(b, a) for a, b in _PAIRS}


def confusable(a, b):
    """True if two equal-length IDs differ only in OCR look-alike characters."""
    return len(a) == len(b) and all(x == y or (x, y) in _PAIRS for x, y in zip(a, b))


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def name_similarity(old, new):
    """0..1. OCR-era names are often truncated or end in stray punctuation, so
    the old name is also compared with the same-length PREFIX of the new one."""
    a, b = _norm(old), _norm(new)
    if not a or not b:
        return 0.0
    full = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) < 12:                       # too short for a prefix match to mean anything
        return full
    return max(full, difflib.SequenceMatcher(None, a, b[:len(a)]).ratio())


def resolve_instrument_id(bad_id, bad_name, known, held_by_same_account=frozenset()):
    """Work out which known-valid ISIN a garbled / placeholder ID should have been.

    known                 {valid_isin: name} - IDs already confirmed by check digit
    held_by_same_account  valid ISINs the same account holds elsewhere; used ONLY
                          to break a tie, never to create a match

    Returns (new_id, how) or (None, reason). Never guesses: if nothing matches,
    or several do and the account cannot choose between them, it says so.
    """
    if bad_id.startswith("NAME:"):
        name = bad_name or bad_id[5:]
        cands = [k for k, n in known.items() if name_similarity(name, n) >= 0.9]
        basis = "name"
    else:
        looks_like = [k for k in known if confusable(bad_id, k)]
        if not looks_like:
            return None, ("no known ISIN is a look-alike - needs a statement that prints the "
                          "exact ISIN (the check digit alone leaves several candidates)")
        # corroborate with the name when there is one
        cands = [k for k in looks_like if not bad_name or name_similarity(bad_name, known[k]) >= 0.6]
        basis = "look-alike ISIN + name"
        if not cands:
            return None, f"look-alike ISIN {looks_like} found but its name disagrees"
    if len(cands) == 1:
        return cands[0], basis
    if not cands:
        return None, "no known fund has a matching name"
    same = [k for k in cands if k in held_by_same_account]
    if len(same) == 1:
        return same[0], f"{basis}; {len(cands)} candidates, only this one is held by the same account"
    return None, f"ambiguous: {len(cands)} candidates {sorted(cands)}"
