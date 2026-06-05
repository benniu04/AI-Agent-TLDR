"""Turn the agent's final JSON into deliverable text.

The agent returns a JSON object (date + sections of {headline, url}). We:
  - parse it defensively (tolerating stray ```json fences or surrounding prose),
  - format it for SMS (plain ASCII, so it stays in cheap 160-char GSM-7 segments),
  - or format it for Telegram (Markdown with tappable links).

Why ASCII matters for SMS: a single non-GSM-7 character (emoji, “smart quotes”, an
em–dash, •) forces the WHOLE message into UCS-2, which bills at 70 chars/segment instead
of 160 — roughly doubling cost. So the SMS formatter hard-sanitizes to ASCII.
"""

import html
import json
import re
import unicodedata

# Per-section emoji for the Telegram digest header.
_SECTION_EMOJI = {"finance": "💰", "ai": "🤖", "tech": "💻", "technology": "💻"}

# Common non-GSM punctuation -> ASCII equivalents (applied before stripping the rest).
_REPLACEMENTS = {
    "—": "-", "–": "-",            # em / en dash
    "‘": "'", "’": "'",            # curly single quotes
    "“": '"', "”": '"',            # curly double quotes
    "…": "...", "•": "-",          # ellipsis, bullet
    "·": "-", "→": "->", "€": "EUR",
    " ": " ",                            # non-breaking space
}


def to_ascii(text: str) -> str:
    """Best-effort GSM-7-safe ASCII: map known punctuation, drop everything else."""
    for bad, good in _REPLACEMENTS.items():
        text = text.replace(bad, good)
    # Decompose accents (café -> cafe), then drop any remaining non-ASCII (emoji, etc.).
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text


def parse_agent_json(text: str) -> dict:
    """Extract the JSON object from the agent's final message.

    Tolerates ```json fences and leading/trailing prose by grabbing the outermost
    {...} span. Raises ValueError if no valid object is found.
    """
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found in agent output")
        candidate = candidate[start : end + 1]
    return json.loads(candidate)


def _norm_url(url: str) -> str:
    """Normalize for dedup comparison (display still uses the original URL)."""
    return url.strip().rstrip("/").lower()


def canonical_url(url: str) -> str:
    """Looser normalization for provenance matching: compare an output URL against the
    URLs search actually returned, tolerating cosmetic differences (scheme, leading www,
    query string, fragment, trailing slash). Used by both the agent (to build the seen-set)
    and the provenance filter, so the two sides normalize identically.
    """
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)      # drop scheme
    u = re.sub(r"^www\.", "", u)          # drop leading www.
    u = u.split("#", 1)[0].split("?", 1)[0]  # drop fragment + query
    return u.rstrip("/")


# Hard backstop for the prompt's "no aggregator/live-blog" rule. The model (especially
# weaker ones) ignores the instruction sometimes, so we drop any URL matching these
# substrings outright — guaranteeing live blogs, daily recaps, and news-aggregator
# bulletins never reach the delivered digest. Patterns are specific enough that a
# dedicated article URL won't match them by accident.
_BANNED_URL_PATTERNS = (
    "live-updates", "live-blog", "liveblog",
    "stock-market-today", "stock-market-update", "market-update",
    "/ai-news", "ai-news-today", "ai-news-brief", "news-briefs", "news-bulletin",
    "/markets/stocks/articles",            # Yahoo daily markets recap stream
    "llm-stats.com", "buildfastwithai.com",  # known aggregator/newsletter blogs
    "youtube.com", "youtu.be",             # videos, not readable articles
    "github.com",                          # code repos/gists, not readable news articles
)


def _is_banned(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _BANNED_URL_PATTERNS)


# Generic filler words that aren't distinctive enough to confirm a headline<->URL match.
_STOPWORDS = frozenset("""
the a an and or to of in on for with at by as after amid over into from its it is are was
new news this that these those will would could may might has have had not but than then
up down off out about more most less least first last next over under top best big major
today day week year report reports say says said unveils unveil launches launch hits hit
set sets amid ahead vs via per inc corp co ltd group plc
earnings revenue revenues shares share stock stocks quarter quarterly guidance results
sales growth market markets price prices profit profits billion million trillion percent
rise rises rose fall falls fell drop drops jump jumps gain gains surge surges plunge slide
""".split())


def _significant_words(text: str) -> set:
    """Distinctive lowercase words (>=4 chars, non-stopword) usable to confirm a URL match."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) >= 4 and w not in _STOPWORDS}


# Two headlines sharing this many distinctive words are treated as the same underlying topic.
_TOPIC_OVERLAP_MIN = 2


def _topic_words(text: str) -> set:
    """Distinctive non-numeric words for same-topic detection (drop pure numbers/years so
    a shared '2026' doesn't count toward the overlap)."""
    return {w for w in _significant_words(text) if not w.isdigit()}


def _is_index_only(url: str) -> bool:
    """True for bare section/index pages (domain root or a single generic section), which
    are not dedicated articles (e.g. openai.com/news/, example.com/blog)."""
    return bool(re.match(
        r"^https?://[^/]+/(news|blog|press|press-releases|newsroom|articles|index|home)?/?$",
        url.strip(), re.I))


def _headline_matches_url(headline: str, url: str) -> bool:
    """Conservative relevance check: keep unless NONE of the headline's significant words
    appear anywhere in the URL. Catches a wrong URL pasted on a headline (e.g. a SpaceX
    headline linking to an Anthropic article) while passing any genuinely related link."""
    u = url.lower()
    # Trust government primary sources — they use opaque coded slugs (e.g.
    # bls.gov/news.release/empsit.nr0.htm) that share no words with the headline.
    host = re.sub(r"^https?://", "", u).split("/", 1)[0]
    if host.endswith(".gov") or host.endswith(".gov/"):
        return True
    words = _significant_words(headline)
    if not words:
        return True  # nothing distinctive to check — don't risk a false drop
    return any(w in u for w in words)


def _iter_sections(data: dict, max_per_section: int, allowed_urls=None, stale_urls=None):
    """Yield (name, items) per section after the link-integrity filter chain.

    `allowed_urls`, when truthy, is the set of canonical URLs that search actually returned
    this run; any item whose URL isn't in it was fabricated by the model and is dropped.
    `stale_urls`, when truthy, is the set of canonical URLs whose publish date is older than
    the recency cutoff. Both fail open: a falsy set means that check is skipped.
    """
    seen_urls = set()      # exact-URL dedup
    kept_topics = []       # topic-word sets of kept items (cross-section), for topic dedup
    for section in data.get("sections", []):
        name = section.get("name", "").strip()
        items = []
        for it in section.get("items", []):
            headline, url = it.get("headline"), it.get("url")
            if not headline or not url:
                continue
            if _is_banned(url) or _is_index_only(url):
                continue  # aggregator / live-blog / recap / bare index page — drop outright
            if not _headline_matches_url(headline, url):
                continue  # URL doesn't match this headline (wrong article) — drop
            if allowed_urls and canonical_url(url) not in allowed_urls:
                continue  # URL never appeared in search results (fabricated) — drop
            if stale_urls and canonical_url(url) in stale_urls:
                continue  # publish date older than the recency cutoff — drop
            key = _norm_url(url)
            if key in seen_urls:
                continue  # duplicate source — skip this headline
            topic = _topic_words(headline)
            if any(len(topic & kept) >= _TOPIC_OVERLAP_MIN for kept in kept_topics):
                continue  # same underlying story as one already kept (diff URL) — drop
            seen_urls.add(key)
            kept_topics.append(topic)
            items.append(it)
            if len(items) >= max_per_section:
                break
        if name and items:
            yield name, items


def delivered_count(data: dict, max_per_section: int = 5, allowed_urls=None, stale_urls=None) -> int:
    """How many items survive the filter chain — for reporting dropped counts."""
    return sum(len(items) for _, items in _iter_sections(data, max_per_section, allowed_urls, stale_urls))


def format_sms(data: dict, max_per_section: int = 5, allowed_urls=None, stale_urls=None) -> str:
    """Plain-ASCII headlines + links, grouped by section. Glanceable, tappable."""
    date = to_ascii(str(data.get("date", ""))).strip()
    lines = [f"TLDR {date}".strip()]
    for name, items in _iter_sections(data, max_per_section, allowed_urls, stale_urls):
        lines.append("")
        lines.append(to_ascii(name).upper())
        for it in items:
            lines.append(f"- {to_ascii(it['headline']).strip()}")
            lines.append(f"  {it['url'].strip()}")  # URLs are already ASCII
    return "\n".join(lines)


def format_telegram(data: dict, max_per_section: int = 5, allowed_urls=None, stale_urls=None) -> str:
    """Clean & minimal HTML digest: emoji section headers, bold titles, linked bullets.

    Uses Telegram HTML (send with parse_mode='HTML') — more robust than Markdown, which
    breaks on headlines/URLs containing _ * [ ] ( ). All text is HTML-escaped.
    """
    date = html.escape(str(data.get("date", "")).strip())
    lines = ["📰 <b>Daily TLDR</b>"]
    if date:
        lines.append(f"<i>{date}</i>")
    for name, items in _iter_sections(data, max_per_section, allowed_urls, stale_urls):
        emoji = _SECTION_EMOJI.get(name.lower(), "•")
        lines.append("")
        lines.append(f"{emoji} <b>{html.escape(name)}</b>")
        for it in items:
            headline = html.escape(it["headline"].strip())
            url = html.escape(it["url"].strip(), quote=True)
            lines.append(f'• <a href="{url}">{headline}</a>')
    return "\n".join(lines)
