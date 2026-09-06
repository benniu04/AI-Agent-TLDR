"""Deterministic graders for a submitted digest. No network, no API key, no model.

WHY THESE GRADE THE PRE-FILTER SUBMISSION
formatting._iter_sections is a *filter*, not an assertion: a banned URL, a duplicate, a
stale story, a repeat, an over-cap item — each is silently dropped and never reaches
Telegram. So grading the DELIVERED digest would score ~100% on almost every rule below by
construction, and measure nothing.

What's worth measuring is how much correcting the model needed. Each check therefore runs
over the raw `submit_tldr` payload and reports a drop rate: `score = 1 - n_bad/n_total`.
1.0 means the model needed no correction on that rule. A score that falls after a prompt
edit means the model got sloppier — even though the filter still caught it, and even though
the digest that shipped looked fine. That's the signal the safety net hides.

Every rule reuses formatting.py rather than restating it, so a change to the production
filter moves these scores instead of silently diverging from them.
"""

from dataclasses import dataclass, field

import config
from evals.labels import SECTIONS, slug
from formatting import (
    _headline_matches_url,
    _is_banned,
    _is_index_only,
    _norm_url,
    _topic_words,
    _TOPIC_OVERLAP_MIN,
    canonical_url,
)

# How many offending items to keep per check. Enough to see the pattern in a report without
# turning a bad run's JSON into a second copy of the digest.
_MAX_EXAMPLES = 3

# A section with fewer than this many items is "thin". Reported, never asserted — see
# `counts` in grade_run(): prompts.py explicitly prefers a short section to a padded one.
_THIN_SECTION = 4


@dataclass
class Check:
    name: str
    score: float                       # 0-1, higher is better
    n_bad: int
    n_total: int
    examples: list = field(default_factory=list)
    note: str = ""                     # set when a check couldn't run (fail-open)

    @property
    def passed(self) -> bool:
        return self.n_bad == 0

    def to_dict(self) -> dict:
        out = {"name": self.name, "passed": self.passed, "score": round(self.score, 4),
               "n_bad": self.n_bad, "n_total": self.n_total, "examples": self.examples}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Row:
    """One structurally valid submitted item, with the section it was submitted under."""
    section_name: str
    section_slug: str | None
    headline: str
    url: str


def _rate(n_bad: int, n_total: int) -> float:
    """Drop rate as a higher-is-better score. An empty digest scores 1.0 on every rule —
    that's why `counts.items` is reported alongside, and why the digest suite is never the
    only thing gating a run."""
    return 1.0 if n_total == 0 else 1.0 - (n_bad / n_total)


def _example(row: Row, why: str) -> dict:
    return {"section": row.section_name, "headline": row.headline[:90],
            "url": row.url[:160], "why": why}


def _rows(data: dict) -> tuple[list[Row], int]:
    """Flatten the submission into valid rows, plus a count of malformed ones.

    Mirrors _iter_sections' tolerance: `or []` for null sections/items, and an item needs
    both a non-empty headline and url to be gradeable at all.
    """
    rows, malformed = [], 0
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            malformed += 1
            continue
        name = str(section.get("name") or "").strip()
        for it in section.get("items") or []:
            if not isinstance(it, dict):
                malformed += 1
                continue
            headline, url = it.get("headline"), it.get("url")
            if not headline or not url or not isinstance(headline, str) or not isinstance(url, str):
                malformed += 1
                continue
            rows.append(Row(name, slug(name), headline.strip(), url.strip()))
    return rows, malformed


# --- individual checks -------------------------------------------------------------------


def _check_schema(data: dict, rows: list[Row], malformed: int) -> Check:
    """submit_tldr is not declared `strict: true`, so nothing server-side guarantees the
    shape. A malformed item is invisible in production — _iter_sections skips it."""
    if not isinstance(data.get("sections"), list):
        return Check("schema_valid", 0.0, 1, 1, [{"why": "no 'sections' list in submission"}])
    total = len(rows) + malformed
    return Check("schema_valid", _rate(malformed, total), malformed, total,
                 [{"why": f"{malformed} item(s) missing a headline or url"}] if malformed else [])


def _check_section_names(data: dict) -> Check:
    """An unrecognized section name isn't an error anywhere in production — it just renders
    with a bullet instead of an emoji, and the evals can't score it. Catch it here."""
    names = [str(s.get("name") or "").strip()
             for s in (data.get("sections") or []) if isinstance(s, dict)]
    bad = [n for n in names if slug(n) is None]
    return Check("section_names", _rate(len(bad), len(names)), len(bad), len(names),
                 [{"why": f"unrecognized section name {n!r}"} for n in bad[:_MAX_EXAMPLES]])


def _check_section_order(data: dict) -> Check:
    """prompts.py demands Finance, Money Movement, Liquidity, AI, Tech in that order, and
    nothing in production enforces it. Whole-digest pass/fail — order isn't per-item."""
    order = [slug(str(s.get("name") or "")) for s in (data.get("sections") or [])
             if isinstance(s, dict)]
    got = [s for s in order if s in SECTIONS]
    want = [s for s in SECTIONS if s in got]
    ok = got == want
    return Check("section_order", 1.0 if ok else 0.0, 0 if ok else 1, 1,
                 [] if ok else [{"why": f"submitted {got}, expected {want}"}])


def _check_no_banned_urls(rows: list[Row]) -> Check:
    bad = [r for r in rows if _is_banned(r.url) or _is_index_only(r.url)]
    return Check("no_banned_urls", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "live-blog / recap / aggregator / bare index page")
                  for r in bad[:_MAX_EXAMPLES]])


def _check_headline_url_match(rows: list[Row]) -> Check:
    bad = [r for r in rows if not _headline_matches_url(r.headline, r.url)]
    return Check("headline_url_match", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "no headline word appears in the URL") for r in bad[:_MAX_EXAMPLES]])


def _check_link_provenance(rows: list[Row], seen_urls: set) -> Check:
    """A URL the run never actually saw was invented by the model. Fails open on an empty
    allowlist, exactly as production does (`if allowed_urls and ...`)."""
    if not seen_urls:
        return Check("link_provenance", 1.0, 0, len(rows), [],
                     note="no provenance allowlist in bundle — check skipped (fails open)")
    bad = [r for r in rows if canonical_url(r.url) not in seen_urls]
    return Check("link_provenance", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "URL never returned by any tool this run") for r in bad[:_MAX_EXAMPLES]])


def _check_no_duplicate_urls(rows: list[Row]) -> Check:
    seen, bad = set(), []
    for r in rows:
        key = _norm_url(r.url)
        if key in seen:
            bad.append(r)
        seen.add(key)
    return Check("no_duplicate_urls", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "same URL already used by an earlier headline")
                  for r in bad[:_MAX_EXAMPLES]])


def _check_no_cross_section_topic(rows: list[Row]) -> Check:
    """Mirrors the per-section dedup exemption at formatting.py:145 — Money Movement and
    Liquidity are deduped WITHIN their own section, not against the global pool, because
    they emit after Finance and were being starved by shared finance buzzwords. Grading
    them globally would report failures production deliberately allows.
    """
    per_section = {"money_movement", "liquidity"}
    global_topics: list[set] = []
    local_topics: dict[str, list[set]] = {}
    bad = []
    for r in rows:
        kept = (local_topics.setdefault(r.section_slug, [])
                if r.section_slug in per_section else global_topics)
        topic = _topic_words(r.headline)
        if any(len(topic & k) >= _TOPIC_OVERLAP_MIN for k in kept):
            bad.append(r)
            continue
        kept.append(topic)
    return Check("no_cross_section_topic", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "same underlying story as an item already kept")
                  for r in bad[:_MAX_EXAMPLES]])


def _check_recency(rows: list[Row], url_ts: dict, now_ts: float) -> Check:
    """Items with no known publish time fail open — production can only build its stale set
    from timestamps it actually has, so grading harder than that would be dishonest."""
    cutoff = now_ts - config.MAX_STORY_AGE_DAYS * 86400
    dated = [r for r in rows if url_ts.get(canonical_url(r.url))]
    bad = [r for r in dated if url_ts[canonical_url(r.url)] < cutoff]
    note = "" if dated else "no publish timestamps in bundle — check skipped (fails open)"
    return Check("recency", _rate(len(bad), len(dated)), len(bad), len(dated),
                 [_example(r, f"published >{config.MAX_STORY_AGE_DAYS}d before the run")
                  for r in bad[:_MAX_EXAMPLES]], note=note)


def _check_no_repeats(rows: list[Row], repeat_urls: set) -> Check:
    if not repeat_urls:
        return Check("no_repeats", 1.0, 0, len(rows), [],
                     note="no cross-run memory in bundle — check skipped (fails open)")
    bad = [r for r in rows if canonical_url(r.url) in repeat_urls]
    return Check("no_repeats", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, "already delivered in a previous run") for r in bad[:_MAX_EXAMPLES]])


def _check_caps_respected(rows: list[Row], caps: dict) -> Check:
    """Production truncates at the cap, so overshoot is invisible after the fact. Pre-filter
    it tells you the model ignored an instruction it was given."""
    default = caps.get("default", config.MAX_HEADLINES_PER_SECTION)
    counts: dict[str, int] = {}
    bad = []
    for r in rows:
        counts[r.section_name] = counts.get(r.section_name, 0) + 1
        cap = caps.get(r.section_name.lower(), default)
        if counts[r.section_name] > cap:
            bad.append(r)
    return Check("caps_respected", _rate(len(bad), len(rows)), len(bad), len(rows),
                 [_example(r, f"beyond the cap for {r.section_name!r}") for r in bad[:_MAX_EXAMPLES]])


# --- entry point -------------------------------------------------------------------------


def grade_run(bundle: dict) -> dict:
    """Grade one run bundle. Returns {"checks", "metrics", "counts"}.

    `metrics` holds only 0-1 higher-is-better rates, so the regression gate can compare them
    generically without per-metric direction config. Anything that isn't on that scale
    (item counts, thin sections) goes in `counts` and is never gated — in particular section
    fill, which prompts.py:75 explicitly refuses to treat as a target.
    """
    data = bundle.get("data") or {}
    seen_urls = set(bundle.get("seen_urls") or [])
    repeat_urls = set(bundle.get("repeat_urls") or [])
    url_ts = {k: v for k, v in (bundle.get("url_ts") or {}).items() if v}
    now_ts = bundle.get("now_ts") or 0.0
    caps = bundle.get("caps") or {}

    rows, malformed = _rows(data)

    checks = [
        _check_schema(data, rows, malformed),
        _check_section_names(data),
        _check_section_order(data),
        _check_no_banned_urls(rows),
        _check_headline_url_match(rows),
        _check_link_provenance(rows, seen_urls),
        _check_no_duplicate_urls(rows),
        _check_no_cross_section_topic(rows),
        _check_recency(rows, url_ts, now_ts),
        _check_no_repeats(rows, repeat_urls),
        _check_caps_respected(rows, caps),
    ]

    metrics = {c.name: round(c.score, 4) for c in checks}
    metrics["digest_integrity"] = round(sum(c.score for c in checks) / len(checks), 4)

    per_section = {}
    for r in rows:
        per_section[r.section_name] = per_section.get(r.section_name, 0) + 1

    return {
        "checks": [c.to_dict() for c in checks],
        "metrics": metrics,
        "counts": {
            "items": len(rows),
            "malformed_items": malformed,
            "sections": len(per_section),
            "items_per_section": per_section,
            "sections_below_4": sum(1 for n in per_section.values() if n < _THIN_SECTION),
            "checks_failed": sum(1 for c in checks if not c.passed),
        },
    }
