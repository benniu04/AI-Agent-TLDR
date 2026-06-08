"""Unit tests for tools.py helpers that don't need network or API keys."""

import time

from tools import _keyword_re, _cap_per_source, _PAYMENTS_KEYWORDS, _LIQUIDITY_KEYWORDS


def _matches(text, keywords):
    return bool(_keyword_re(tuple(keywords)).search(text))


# --------------------------------------------------------------------------- keyword matching

def test_keyword_left_boundary_blocks_embedded_substrings():
    kw = ("visa", "scam", "rtp")
    # mid-word substrings must NOT match (the bug we fixed)
    assert not _matches("New revisable contract terms", kw)   # 'visa' inside 'revisable'
    assert not _matches("Quarterly results steady", kw)       # no keyword
    # standalone / start-of-word DOES match
    assert _matches("Visa launches new rails", kw)
    assert _matches("Visa's new product", kw)                 # possessive


def test_keyword_preserves_inflections():
    # left-boundary keeps prefixes: 'scam'->'scams', 'fed'->'Federal', 'deposit'->'deposits'
    assert _matches("Zelle scams surge this quarter", ("scam",))
    assert _matches("Federal Reserve holds rates", ("fed",))
    assert _matches("Bank deposits climb", ("deposit",))


def test_keyword_multiword_and_hyphenated():
    assert _matches("FedNow real-time payment volume grows", ("real-time payment",))
    assert _matches("New card network fees announced", ("card network",))


def test_real_keyword_sets_compile_and_match_examples():
    assert _matches("Zelle fraud reimbursement rules tighten", _PAYMENTS_KEYWORDS)
    assert _matches("2-year Treasury yield jumps", _LIQUIDITY_KEYWORDS)


# --------------------------------------------------------------------------- per-source cap

def test_cap_per_source_limits_each_source():
    now = time.time()
    items = ([{"source": "A", "url": f"https://a/{i}", "ts": now - i} for i in range(6)] +
             [{"source": "B", "url": f"https://b/{i}", "ts": now - i} for i in range(6)])
    out = _cap_per_source(items, per_source=2, total=10)
    by_src = {}
    for it in out:
        by_src[it["source"]] = by_src.get(it["source"], 0) + 1
    assert by_src == {"A": 2, "B": 2}


def test_cap_per_source_sourceless_items_use_url_fallback():
    now = time.time()
    # No 'source' key: must NOT all collapse into one "" bucket (distinct URLs -> distinct keys)
    items = [{"url": f"https://x/{i}", "ts": now - i} for i in range(4)]
    out = _cap_per_source(items, per_source=2, total=10)
    assert len(out) == 4  # each distinct URL kept, not capped to 2 under a shared "" key
