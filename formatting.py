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
)


def _is_banned(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in _BANNED_URL_PATTERNS)


def _iter_sections(data: dict, max_per_section: int):
    """Yield (name, items) per section, dropping any headline whose URL was already
    used anywhere in the digest — so no two delivered headlines link to the same page.
    """
    seen_urls = set()
    for section in data.get("sections", []):
        name = section.get("name", "").strip()
        items = []
        for it in section.get("items", []):
            headline, url = it.get("headline"), it.get("url")
            if not headline or not url:
                continue
            if _is_banned(url):
                continue  # aggregator / live-blog / recap — drop outright
            key = _norm_url(url)
            if key in seen_urls:
                continue  # duplicate source — skip this headline
            seen_urls.add(key)
            items.append(it)
            if len(items) >= max_per_section:
                break
        if name and items:
            yield name, items


def format_sms(data: dict, max_per_section: int = 5) -> str:
    """Plain-ASCII headlines + links, grouped by section. Glanceable, tappable."""
    date = to_ascii(str(data.get("date", ""))).strip()
    lines = [f"TLDR {date}".strip()]
    for name, items in _iter_sections(data, max_per_section):
        lines.append("")
        lines.append(to_ascii(name).upper())
        for it in items:
            lines.append(f"- {to_ascii(it['headline']).strip()}")
            lines.append(f"  {it['url'].strip()}")  # URLs are already ASCII
    return "\n".join(lines)


def format_telegram(data: dict, max_per_section: int = 5) -> str:
    """Clean & minimal HTML digest: emoji section headers, bold titles, linked bullets.

    Uses Telegram HTML (send with parse_mode='HTML') — more robust than Markdown, which
    breaks on headlines/URLs containing _ * [ ] ( ). All text is HTML-escaped.
    """
    date = html.escape(str(data.get("date", "")).strip())
    lines = ["📰 <b>Daily TLDR</b>"]
    if date:
        lines.append(f"<i>{date}</i>")
    for name, items in _iter_sections(data, max_per_section):
        emoji = _SECTION_EMOJI.get(name.lower(), "•")
        lines.append("")
        lines.append(f"{emoji} <b>{html.escape(name)}</b>")
        for it in items:
            headline = html.escape(it["headline"].strip())
            url = html.escape(it["url"].strip(), quote=True)
            lines.append(f'• <a href="{url}">{headline}</a>')
    return "\n".join(lines)
