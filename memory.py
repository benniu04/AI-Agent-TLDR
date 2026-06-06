"""Cross-run memory: remember what we've already delivered so we don't repeat stories.

GitHub Actions is ephemeral, so this file is committed back to the repo by the workflow
after each run (see .github/workflows/daily.yml). It holds a rolling window of the last
MEMORY_KEEP_DAYS of delivered items, used two ways:
  - HARD backstop: drop any item whose URL we already delivered (deterministic, $0). This is
    what finally kills the recurring-stale story (e.g. an IPO filing that keeps resurfacing in
    web_search results with no timestamp, so the recency filter can't catch it).
  - SOFT signal: the recent headlines are fed into the agent's goal so it skips stories
    already covered unless there's a genuinely new development.

Entries are {url, headline, date, ts}, where `ts` is the epoch time WE delivered the item
(not the article's publish time) — so the window prunes by how long ago we showed it.
"""

import json
import os

from formatting import canonical_url


def load_entries(path: str) -> list:
    """Load the memory file -> list of {url, headline, date, ts}. Missing/bad file -> []."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def seen_url_set(entries: list) -> set:
    """Canonical URLs we've already delivered (for the hard repeat filter)."""
    return {canonical_url(e["url"]) for e in entries if e.get("url")}


def recent_headlines(entries: list, cutoff_ts: float, limit: int = 40) -> list:
    """Headlines delivered since cutoff_ts, newest first, capped — for the agent's goal."""
    recent = [e for e in entries if e.get("headline") and (e.get("ts") or 0) >= cutoff_ts]
    recent.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return [e["headline"] for e in recent[:limit]]


def update(path: str, delivered: list, run_iso: str, run_ts: float, keep_days: int) -> int:
    """Append today's delivered items, de-dupe by URL, prune to the window, write back.

    Returns the number of entries kept. Pruning drops anything we delivered more than
    keep_days ago, so the file stays small and the repeat filter only blocks recent stories.
    """
    entries = load_entries(path)
    for it in delivered:
        url, headline = it.get("url"), it.get("headline")
        if url and headline:
            entries.append({"url": url, "headline": headline, "date": run_iso, "ts": run_ts})

    cutoff = run_ts - keep_days * 86400
    by_url = {}  # canonical url -> newest entry within the window
    for e in sorted(entries, key=lambda e: e.get("ts") or 0):  # oldest first; newest wins
        if not e.get("url") or (e.get("ts") or 0) < cutoff:
            continue
        by_url[canonical_url(e["url"])] = e
    kept = sorted(by_url.values(), key=lambda e: e.get("ts") or 0, reverse=True)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(kept, fh, indent=2, ensure_ascii=False)
    return len(kept)
