"""Turn the agent's final JSON into deliverable text.

The agent returns a JSON object (date + sections of {headline, url}). We:
  - parse it defensively (tolerating stray ```json fences or surrounding prose),
  - format it for SMS (plain ASCII, so it stays in cheap 160-char GSM-7 segments),
  - or format it for Telegram (Markdown with tappable links).

Why ASCII matters for SMS: a single non-GSM-7 character (emoji, “smart quotes”, an
em–dash, •) forces the WHOLE message into UCS-2, which bills at 70 chars/segment instead
of 160 — roughly doubling cost. So the SMS formatter hard-sanitizes to ASCII.
"""

import json
import re
import unicodedata

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
    """Markdown headlines with inline links (free fallback channel)."""
    date = str(data.get("date", "")).strip()
    lines = [f"*📰 TLDR — {date}*"]
    for name, items in _iter_sections(data, max_per_section):
        lines.append("")
        lines.append(f"*{name}*")
        for it in items:
            lines.append(f"• [{it['headline'].strip()}]({it['url'].strip()})")
    return "\n".join(lines)
