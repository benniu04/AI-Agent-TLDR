"""Unit tests for the link-integrity / formatting pipeline (formatting.py).

These cover the deterministic layer that decides what actually ships after the agent
submits a briefing — the part that guarantees recency, link integrity, and dedup
regardless of model behavior. All pure functions: no network, no API keys.

Run:  .venv/bin/pytest -q
"""

import pytest

from formatting import (
    canonical_url,
    delivered_count,
    format_telegram,
    parse_agent_json,
    _headline_matches_url,
    _is_banned,
    _is_index_only,
    _iter_sections,
    _norm_url,
    _topic_words,
)


def _section(name, items):
    return {"name": name, "items": items}


def _item(headline, url):
    return {"headline": headline, "url": url}


# --------------------------------------------------------------------------- URL canonicalization

@pytest.mark.parametrize("url, expected", [
    ("https://www.reuters.com/markets/fed-decision/", "reuters.com/markets/fed-decision"),
    ("http://reuters.com/markets/fed-decision", "reuters.com/markets/fed-decision"),
    ("https://reuters.com/markets/fed-decision?utm_source=x#top", "reuters.com/markets/fed-decision"),
    ("HTTPS://WWW.Reuters.com/Markets/Fed-Decision", "reuters.com/markets/fed-decision"),
])
def test_canonical_url_ignores_cosmetic_differences(url, expected):
    assert canonical_url(url) == expected


def test_canonical_url_treats_variants_as_equal():
    a = canonical_url("https://www.example.com/story?ref=feed")
    b = canonical_url("http://example.com/story/")
    assert a == b


def test_norm_url_keeps_query_for_strict_dedup():
    # _norm_url is the stricter dedup key; it lowercases + strips trailing slash but keeps query.
    assert _norm_url("https://x.com/a/") == "https://x.com/a"
    assert _norm_url("https://x.com/a?b=1") != _norm_url("https://x.com/a")


# --------------------------------------------------------------------------- banned / index pages

@pytest.mark.parametrize("url", [
    "https://bloomberg.com/news/live-updates/markets",
    "https://cnbc.com/2026/06/05/stock-market-today.html",
    "https://llm-stats.com/leaderboard",
    "https://youtube.com/watch?v=abc",
    "https://github.com/org/repo",
])
def test_is_banned_catches_aggregators_and_nonarticles(url):
    assert _is_banned(url) is True


@pytest.mark.parametrize("url", [
    "https://reuters.com/business/finance/specific-real-story-2026-06-05",
    "https://techcrunch.com/2026/06/05/startup-raises-series-b",
])
def test_is_banned_passes_real_articles(url):
    assert _is_banned(url) is False


@pytest.mark.parametrize("url", [
    "https://openai.com/news/",
    "https://example.com/blog",
    "https://site.com/",
])
def test_is_index_only_flags_bare_section_pages(url):
    assert _is_index_only(url) is True


def test_is_index_only_passes_dedicated_article():
    assert _is_index_only("https://openai.com/news/gpt-launch-2026") is False


# --------------------------------------------------------------------------- headline <-> URL match

def test_headline_url_mismatch_is_dropped():
    # A SpaceX headline pointing at an Anthropic article shares no significant words.
    assert _headline_matches_url("SpaceX prices record IPO", "https://reuters.com/anthropic-ipo") is False


def test_headline_url_match_passes():
    assert _headline_matches_url("SpaceX prices record IPO", "https://reuters.com/spacex-ipo-pricing") is True


def test_gov_sources_are_trusted_despite_opaque_slugs():
    # BLS uses coded slugs that share no words with the headline — must not be dropped.
    assert _headline_matches_url("May jobs report shows strong hiring",
                                 "https://bls.gov/news.release/empsit.nr0.htm") is True


def test_topic_words_drops_stopwords_and_numbers():
    words = _topic_words("The Fed raises rates by 25 in 2026")
    assert "raises" in words and "rates" in words  # distinctive >=4-char words
    assert "2026" not in words   # pure numbers dropped
    assert "the" not in words    # stopword dropped
    assert "fed" not in words    # under the 4-char threshold


# --------------------------------------------------------------------------- the filter chain

def _delivered(data, cap=5, allowed=None, stale=None):
    return [it for _, items in _iter_sections(data, cap, allowed, stale) for it in items]


def test_clean_digest_passes_through():
    data = {"sections": [_section("Finance", [
        _item("Acme beats Q2 earnings", "https://reuters.com/acme-q2-earnings"),
        _item("Beta cuts full-year outlook", "https://wsj.com/beta-guidance-cut"),
    ])]}
    assert delivered_count(data) == 2


def test_banned_and_index_items_are_dropped():
    data = {"sections": [_section("Finance", [
        _item("Markets live", "https://bloomberg.com/news/live-updates/x"),
        _item("News index", "https://openai.com/news/"),
        _item("Real story", "https://reuters.com/real-specific-story"),
    ])]}
    assert delivered_count(data) == 1


def test_provenance_drops_unseen_urls_but_fails_open():
    # Both items' URLs match their own headlines (so the keyword filter passes them);
    # the only thing separating them is whether the URL was actually seen in results.
    data = {"sections": [_section("AI", [
        _item("Model ships today", "https://openai.com/model-ships"),
        _item("Imaginary product launches", "https://fake.com/imaginary-product"),
    ])]}
    allowed = {canonical_url("https://openai.com/model-ships")}
    # With an allowlist: only the seen URL survives.
    assert delivered_count(data, allowed_urls=allowed) == 1
    # Fail-open: no allowlist means provenance check is skipped entirely.
    assert delivered_count(data, allowed_urls=None) == 2
    assert delivered_count(data, allowed_urls=set()) == 2


def test_provenance_tolerates_cosmetic_url_differences():
    data = {"sections": [_section("AI", [
        _item("Model ships", "https://www.openai.com/model-ships/?utm=feed"),
    ])]}
    allowed = {canonical_url("http://openai.com/model-ships")}
    assert delivered_count(data, allowed_urls=allowed) == 1


def test_stale_urls_are_dropped_but_fails_open():
    data = {"sections": [_section("Finance", [
        _item("Old filing resurfaces", "https://reuters.com/old-filing"),
        _item("Fresh news", "https://reuters.com/fresh-news"),
    ])]}
    stale = {canonical_url("https://reuters.com/old-filing")}
    assert delivered_count(data, stale_urls=stale) == 1
    assert delivered_count(data, stale_urls=None) == 2


def test_exact_duplicate_url_is_dropped():
    data = {"sections": [_section("Tech", [
        _item("Story A", "https://verge.com/story"),
        _item("Story A again", "https://verge.com/story"),
    ])]}
    assert delivered_count(data) == 1


def test_topic_dedup_collapses_same_event_across_sections():
    # Same Fed event submitted in two sections (different URLs) -> kept once total.
    data = {"sections": [
        _section("Finance", [_item("Federal Reserve holds interest rates steady",
                                   "https://wsj.com/fed-holds-rates")]),
        _section("Liquidity", [_item("Federal Reserve interest rates unchanged at meeting",
                                     "https://reuters.com/fed-rates-decision")]),
    ]}
    assert delivered_count(data) == 1


def test_per_section_cap_is_enforced():
    # 8 topically-distinct items with matching URLs (so only the cap, not dedup/keyword, trims).
    items = [
        _item("Apple unveils foldable laptop", "https://reuters.com/apple-foldable-laptop"),
        _item("Tesla recalls pickup fleet", "https://reuters.com/tesla-pickup-recall"),
        _item("Spotify expands podcast tools", "https://reuters.com/spotify-podcast-tools"),
        _item("Nvidia announces gaming chip", "https://reuters.com/nvidia-gaming-chip"),
        _item("Reddit redesigns mobile interface", "https://reuters.com/reddit-mobile-redesign"),
        _item("Dropbox shutters legacy service", "https://reuters.com/dropbox-legacy-shutdown"),
        _item("Figma rebuilds plugin engine", "https://reuters.com/figma-plugin-engine"),
        _item("Stripe enters lending business", "https://reuters.com/stripe-lending-launch"),
    ]
    data = {"sections": [_section("Tech", items)]}
    assert delivered_count(data, 5) == 5


def test_items_missing_headline_or_url_are_skipped():
    data = {"sections": [_section("Finance", [
        {"headline": "No url"},
        {"url": "https://reuters.com/no-headline"},
        _item("Good one", "https://reuters.com/good-one"),
    ])]}
    assert delivered_count(data) == 1


def test_null_items_and_sections_do_not_crash():
    # The agent can emit JSON null for items/sections; .get(...) returns None, not [].
    assert delivered_count({"sections": None}) == 0
    assert delivered_count({"sections": [{"name": "Finance", "items": None}]}) == 0
    mixed = {"sections": [
        {"name": "Finance", "items": None},
        _section("Tech", [_item("Real story", "https://verge.com/real-story")]),
    ]}
    assert delivered_count(mixed) == 1


# --------------------------------------------------------------------------- Telegram formatting

def test_telegram_renders_sections_links_and_emoji():
    data = {"date": "Fri, Jun 5", "sections": [
        _section("Finance", [_item("Acme beats Q2", "https://reuters.com/acme-q2")]),
        _section("Money Movement", [_item("BofA joins deposit network", "https://finextra.com/bofa-deposits")]),
        _section("Liquidity", [_item("Yields jump on payrolls", "https://wsj.com/yields-payrolls")]),
    ]}
    out = format_telegram(data)
    assert "\U0001F4B0" in out and "\U0001F4B8" in out and "\U0001F30A" in out  # 💰 💸 🌊
    assert '<a href="https://reuters.com/acme-q2">Acme beats Q2</a>' in out
    assert "<b>Money Movement</b>" in out


def test_telegram_escapes_html_in_headlines():
    data = {"sections": [_section("Tech", [
        _item("AT&T & <Verizon> lose case", "https://verge.com/att-verizon"),
    ])]}
    out = format_telegram(data)
    assert "&amp;" in out and "&lt;Verizon&gt;" in out
    assert "<Verizon>" not in out


# --------------------------------------------------------------------------- agent JSON parsing

def test_parse_agent_json_raw_object():
    assert parse_agent_json('{"a": 1}') == {"a": 1}


def test_parse_agent_json_fenced():
    assert parse_agent_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_agent_json_surrounded_by_prose():
    assert parse_agent_json('Here is the briefing:\n{"a": 1}\nDone.') == {"a": 1}


def test_parse_agent_json_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_agent_json("no json here at all")
