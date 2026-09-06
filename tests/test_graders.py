"""Unit tests for the deterministic digest graders (evals/graders.py).

Each test plants exactly one known violation and asserts the matching check catches it and
the others stay clean — so a broken grader shows up as a specific failure rather than a
vaguely lower score. All pure functions: no network, no API keys, no model.

Run:  .venv/bin/pytest -q
"""

import pytest

from evals.graders import grade_run


def _item(headline, url):
    return {"headline": headline, "url": url}


def _bundle(sections, **ctx):
    """A run bundle with the five sections' worth of context defaulted to fail-open."""
    return {
        "schema": 1,
        "now_ts": 1_780_000_000.0,
        "data": {"date": "Fri, Jun 5", "sections": sections},
        "seen_urls": ctx.get("seen_urls", []),
        "url_ts": ctx.get("url_ts", {}),
        "repeat_urls": ctx.get("repeat_urls", []),
        "caps": ctx.get("caps", {"default": 5, "money movement": 8, "liquidity": 8}),
    }


def _clean_sections():
    """A submission that violates nothing: distinct topics, dedicated article URLs, each
    headline sharing a distinctive word with its own link."""
    return [
        {"name": "Finance", "items": [
            _item("Broadcom tumbles after chip forecast disappoints",
                  "https://example.com/broadcom-chip-forecast"),
            _item("Quantinuum prices upsized quantum listing",
                  "https://example.com/quantinuum-quantum-listing"),
        ]},
        {"name": "Money Movement", "items": [
            _item("Zelle rolls out scam reimbursement policy",
                  "https://example.com/zelle-scam-reimbursement"),
        ]},
        {"name": "Liquidity", "items": [
            _item("Treasury yields climb after auction demand weakens",
                  "https://example.com/treasury-yields-auction"),
        ]},
        {"name": "AI", "items": [
            _item("Microsoft unveils MAI reasoning models",
                  "https://example.com/microsoft-mai-models"),
        ]},
        {"name": "Tech", "items": [
            _item("Cloudflare acquires VoidZero developer tooling",
                  "https://example.com/cloudflare-voidzero-tooling"),
        ]},
    ]


def _score(result, name):
    return result["metrics"][name]


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_clean_digest_scores_perfectly():
    result = grade_run(_bundle(_clean_sections()))
    assert result["metrics"]["digest_integrity"] == 1.0
    assert result["counts"]["checks_failed"] == 0
    assert result["counts"]["items"] == 6


def test_digest_integrity_is_the_mean_of_the_checks():
    result = grade_run(_bundle(_clean_sections()))
    scores = [c["score"] for c in result["checks"]]
    assert result["metrics"]["digest_integrity"] == pytest.approx(sum(scores) / len(scores), abs=1e-4)


# --- one violation per check --------------------------------------------------------------


def test_schema_valid_catches_an_item_with_no_url():
    sections = _clean_sections()
    sections[0]["items"].append({"headline": "Headline with no link at all"})
    result = grade_run(_bundle(sections))
    assert _check(result, "schema_valid")["n_bad"] == 1
    assert _score(result, "schema_valid") < 1.0


def test_schema_valid_fails_hard_when_sections_is_missing():
    result = grade_run({"data": {"date": "Fri, Jun 5"}, "now_ts": 1_780_000_000.0})
    assert _score(result, "schema_valid") == 0.0


def test_section_names_catches_an_invented_section():
    sections = _clean_sections()
    sections.append({"name": "Crypto", "items": [
        _item("Bitcoin rallies past prior record",
              "https://example.com/bitcoin-rallies-record")]})
    result = grade_run(_bundle(sections))
    assert _check(result, "section_names")["n_bad"] == 1


def test_section_order_catches_a_reordered_digest():
    sections = _clean_sections()
    sections[0], sections[2] = sections[2], sections[0]  # Liquidity ahead of Finance
    result = grade_run(_bundle(sections))
    assert _score(result, "section_order") == 0.0


def test_section_order_tolerates_a_missing_section():
    """A thin day may submit fewer than five sections; that's an ordering non-event."""
    sections = [s for s in _clean_sections() if s["name"] != "Liquidity"]
    result = grade_run(_bundle(sections))
    assert _score(result, "section_order") == 1.0


@pytest.mark.parametrize("url", [
    "https://example.com/live-updates/markets-now",
    "https://finance.yahoo.com/markets/stocks/articles/daily-recap-113626417.html",
    "https://github.com/anthropics/some-repo",
    "https://openai.com/news/",  # bare index page
])
def test_no_banned_urls_catches_each_banned_shape(url):
    sections = _clean_sections()
    sections[0]["items"].append(_item("Broadcom chip forecast recap lands", url))
    result = grade_run(_bundle(sections))
    assert _check(result, "no_banned_urls")["n_bad"] == 1


def test_headline_url_match_catches_a_borrowed_link():
    sections = _clean_sections()
    sections[0]["items"].append(
        _item("Initial jobless claims hit highest level since February",
              "https://example.com/broadcom-avgo-earnings-report"))
    result = grade_run(_bundle(sections))
    assert _check(result, "headline_url_match")["n_bad"] == 1


def test_link_provenance_flags_a_url_no_tool_returned():
    sections = _clean_sections()
    allowed = ["example.com/broadcom-chip-forecast", "example.com/quantinuum-quantum-listing",
               "example.com/zelle-scam-reimbursement", "example.com/treasury-yields-auction",
               "example.com/microsoft-mai-models", "example.com/cloudflare-voidzero-tooling"]
    sections[0]["items"].append(
        _item("Nvidia announces Rubin accelerator", "https://example.com/nvidia-rubin-accelerator"))
    result = grade_run(_bundle(sections, seen_urls=allowed))
    assert _check(result, "link_provenance")["n_bad"] == 1


def test_link_provenance_fails_open_without_an_allowlist():
    """Production skips this filter on an empty allowlist; grading harder than the pipeline
    would report failures that never happened."""
    result = grade_run(_bundle(_clean_sections(), seen_urls=[]))
    assert _score(result, "link_provenance") == 1.0
    assert "fails open" in _check(result, "link_provenance")["note"]


def test_no_duplicate_urls_catches_a_reused_link():
    sections = _clean_sections()
    sections[4]["items"].append(
        _item("Microsoft MAI models reach general availability",
              "https://example.com/microsoft-mai-models"))
    result = grade_run(_bundle(sections))
    assert _check(result, "no_duplicate_urls")["n_bad"] == 1


def test_no_cross_section_topic_catches_the_same_story_twice():
    sections = _clean_sections()
    sections[4]["items"].append(  # Tech repeating the Finance Broadcom story
        _item("Broadcom chip forecast rattles suppliers",
              "https://example.com/broadcom-chip-suppliers"))
    result = grade_run(_bundle(sections))
    assert _check(result, "no_cross_section_topic")["n_bad"] == 1


def test_no_cross_section_topic_honors_the_money_movement_exemption():
    """formatting.py:145 deliberately dedups Money Movement and Liquidity WITHIN section, so
    they aren't starved by shared finance vocabulary. The grader must not report those."""
    sections = _clean_sections()
    sections[1]["items"].append(  # Money Movement, overlapping the Finance Broadcom headline
        _item("Broadcom chip supplier adds instant payout rails",
              "https://example.com/broadcom-chip-payout-rails"))
    result = grade_run(_bundle(sections))
    assert _check(result, "no_cross_section_topic")["n_bad"] == 0


def test_recency_catches_a_stale_story():
    sections = _clean_sections()
    now = 1_780_000_000.0
    url_ts = {"example.com/broadcom-chip-forecast": now - 10 * 86400}  # older than 3 days
    result = grade_run(_bundle(sections, url_ts=url_ts))
    assert _check(result, "recency")["n_bad"] == 1
    assert _check(result, "recency")["n_total"] == 1  # undated items aren't graded


def test_recency_fails_open_with_no_timestamps():
    result = grade_run(_bundle(_clean_sections(), url_ts={}))
    assert _score(result, "recency") == 1.0
    assert "fails open" in _check(result, "recency")["note"]


def test_no_repeats_catches_a_story_already_delivered():
    result = grade_run(_bundle(_clean_sections(),
                               repeat_urls=["example.com/zelle-scam-reimbursement"]))
    assert _check(result, "no_repeats")["n_bad"] == 1


def test_caps_respected_counts_only_the_overshoot():
    sections = _clean_sections()
    sections[0]["items"] = [
        _item(f"Broadcom division {n} posts revised chip outlook",
              f"https://example.com/broadcom-division-{n}") for n in range(7)
    ]
    result = grade_run(_bundle(sections))  # Finance cap is the default 5
    assert _check(result, "caps_respected")["n_bad"] == 2


def test_caps_respected_uses_the_money_movement_override():
    """Money Movement's ceiling is 8, not 5 — grading it at the default would flag four
    perfectly legal items."""
    sections = _clean_sections()
    sections[1]["items"] = [
        _item(f"Zelle partner bank {n} adopts new payout rule",
              f"https://example.com/zelle-partner-{n}") for n in range(8)
    ]
    result = grade_run(_bundle(sections))
    assert _check(result, "caps_respected")["n_bad"] == 0


# --- reporting ----------------------------------------------------------------------------


def test_thin_sections_are_counted_not_penalized():
    """prompts.py:75 says a short section beats a padded one, so fill is reported in `counts`
    and deliberately kept out of the gated `metrics`."""
    result = grade_run(_bundle(_clean_sections()))
    assert result["counts"]["sections_below_4"] == 5
    assert result["metrics"]["digest_integrity"] == 1.0
    assert not any("fill" in k for k in result["metrics"])


def test_examples_are_capped_so_a_bad_run_stays_readable():
    sections = [{"name": "Finance", "items": [
        _item(f"Story number {n} about a market recap",
              f"https://example.com/live-updates/story-{n}") for n in range(10)]}]
    result = grade_run(_bundle(sections))
    assert len(_check(result, "no_banned_urls")["examples"]) == 3
