"""Tool definitions for the agent.

Two kinds of tools live here:

1. **Server tools** (`web_search`, `web_fetch`) — Anthropic executes these on their
   side, inside a single API turn. We never run them ourselves; we only declare them.
   They are the FALLBACK / gap-filler now that we have dedicated source tools.
2. **Custom client tools** — we run these. The agent emits a `tool_use` block, our loop
   calls `dispatch()`, and we feed the result back. These are the PRIMARY sources:
     - `get_finance_news`  — Finnhub + Alpha Vantage (structured, with sentiment scores)
     - `get_tech_news`     — RSS from TechCrunch / The Verge / Ars Technica / VentureBeat
     - `get_ai_news`       — AI-specific RSS + AI-keyword-filtered tech feeds
     - `get_hacker_news`   — HN front page (Algolia API, no key)

All are free (no per-call fee), so leaning on them lowers cost vs web_search ($10/1k).

`TOOLS` is the JSON list handed to the Messages API. `dispatch()` maps a custom tool
name to the Python function that implements it.
"""

import html
import json
import time

import feedparser
import requests

import config

HTTP_TIMEOUT = 12
_UA = "Mozilla/5.0 (compatible; DailyTLDR/1.0)"  # some feeds reject the default UA


# --- RSS sources ------------------------------------------------------------

_TECH_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "VentureBeat": "https://venturebeat.com/feed/",
}
_AI_FEEDS = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
}
_AI_KEYWORDS = (
    "ai", "a.i.", "artificial intelligence", "machine learning", "deep learning",
    "llm", "gpt", "chatgpt", "claude", "gemini", "openai", "anthropic", "neural",
    "model", "agentic", "agent", "transformer", "inference", "nvidia",
)


def _parse_feeds(feeds: dict, limit: int, keywords=None) -> list:
    """Fetch + parse RSS feeds into [{title, url, source, published}], newest first.

    keywords (optional): keep only entries whose title contains one of them (for AI).
    """
    entries = []
    for source, url in feeds.items():
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": _UA})
            parsed = feedparser.parse(resp.content)
        except Exception:
            continue  # one dead feed shouldn't sink the whole tool
        for e in parsed.entries:
            title, link = e.get("title"), e.get("link")
            if not title or not link:
                continue
            title = html.unescape(title)  # RSS titles carry entities like &#8217;
            if keywords and not any(k in title.lower() for k in keywords):
                continue
            entries.append({
                "title": title,
                "url": link,
                "source": source,
                "published": e.get("published", e.get("updated", "")),
                "_ts": time.mktime(e.published_parsed) if e.get("published_parsed") else 0,
            })
    # newest first, dedupe by URL
    entries.sort(key=lambda x: x["_ts"], reverse=True)
    seen, out = set(), []
    for e in entries:
        if e["url"] in seen:
            continue
        seen.add(e["url"])
        out.append({k: e[k] for k in ("title", "url", "source", "published")})
    return out[:limit]


# --- Custom client tool implementations -------------------------------------

def get_tech_news(limit: int = 20) -> str:
    """Top recent technology headlines from major tech outlets (RSS)."""
    return json.dumps(_parse_feeds(_TECH_FEEDS, int(limit)))


def get_ai_news(limit: int = 20) -> str:
    """Recent AI headlines: AI-specific feeds + AI-keyword-filtered general tech feeds."""
    items = _parse_feeds(_AI_FEEDS, int(limit)) + _parse_feeds(_TECH_FEEDS, int(limit), _AI_KEYWORDS)
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return json.dumps(out[: int(limit)])


def get_hacker_news(limit: int = 20) -> str:
    """HN front page via the Algolia API (one call, no key). Returns {title, url, score}."""
    limit = max(1, min(int(limit), 50))
    r = requests.get("https://hn.algolia.com/api/v1/search",
                     params={"tags": "front_page", "hitsPerPage": limit},
                     timeout=HTTP_TIMEOUT, headers={"User-Agent": _UA})
    hits = r.json().get("hits", [])
    stories = []
    for h in hits:
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        stories.append({"title": h.get("title"), "url": url, "score": h.get("points", 0)})
    return json.dumps([s for s in stories if s["title"]])


def _finnhub_news(limit: int) -> list:
    r = requests.get("https://finnhub.io/api/v1/news",
                     params={"category": "general", "token": config.FINNHUB_API_KEY},
                     timeout=HTTP_TIMEOUT, headers={"User-Agent": _UA})
    items = r.json() if isinstance(r.json(), list) else []
    out = []
    for it in items[:limit]:
        out.append({
            "headline": it.get("headline"),
            "url": it.get("url"),
            "source": it.get("source"),
            "datetime": time.strftime("%Y-%m-%d", time.gmtime(it.get("datetime", 0))) if it.get("datetime") else "",
        })
    return [o for o in out if o["headline"] and o["url"]]


def _alphavantage_news(limit: int) -> list:
    r = requests.get("https://www.alphavantage.co/query", timeout=HTTP_TIMEOUT,
                     params={"function": "NEWS_SENTIMENT",
                             "topics": "financial_markets,economy_macro,mergers_and_acquisitions,ipo,earnings",
                             "sort": "LATEST", "limit": limit, "apikey": config.ALPHAVANTAGE_API_KEY})
    feed = r.json().get("feed", [])  # missing/empty when rate-limited
    out = []
    for it in feed[:limit]:
        out.append({
            "headline": it.get("title"),
            "url": it.get("url"),
            "source": it.get("source"),
            "datetime": (it.get("time_published", "")[:8] or ""),
            "sentiment": it.get("overall_sentiment_label"),
            "sentiment_score": it.get("overall_sentiment_score"),
        })
    return [o for o in out if o["headline"] and o["url"]]


def get_finance_news(limit: int = 20) -> str:
    """Structured financial news from Finnhub + Alpha Vantage (with sentiment scores).

    Uses whichever API keys are configured. Raises if neither is set so the agent falls
    back to web_search. Alpha Vantage items include sentiment you can rank/filter by.
    """
    limit = int(limit)
    items = []
    if config.ALPHAVANTAGE_API_KEY:
        items += _alphavantage_news(limit)  # first, so sentiment-bearing items win dedupe
    if config.FINNHUB_API_KEY:
        items += _finnhub_news(limit)
    if not config.ALPHAVANTAGE_API_KEY and not config.FINNHUB_API_KEY:
        raise RuntimeError("No finance API key configured (FINNHUB_API_KEY / ALPHAVANTAGE_API_KEY)")
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return json.dumps(out[:limit])


# Registry of custom (client-executed) tools: name -> callable(**input) -> str
CLIENT_TOOLS = {
    "get_finance_news": get_finance_news,
    "get_tech_news": get_tech_news,
    "get_ai_news": get_ai_news,
    "get_hacker_news": get_hacker_news,
}


def dispatch(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a custom client tool. Returns (content, is_error).

    Tool failures are returned as data (is_error=True), never raised, so the agent
    can adapt instead of the whole run crashing.
    """
    fn = CLIENT_TOOLS.get(name)
    if fn is None:
        return f"Unknown tool: {name}", True
    try:
        return fn(**(tool_input or {})), False
    except Exception as exc:  # noqa: BLE001 - deliberately broad; errors are inputs
        return f"{type(exc).__name__}: {exc}", True


def _custom_tool(name, description, extra_props=None):
    props = {"limit": {"type": "integer", "description": "Max items to return (default 20)."}}
    props.update(extra_props or {})
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": []}}


# --- Tool declarations passed to the API ------------------------------------

TOOLS = [
    # Server tools — FALLBACK / gap-filler only (dedicated source tools are primary and
    # nearly free). Capped low: a run's cost is ~all web_search tokens (~23k each), and the
    # feeds already supply most coverage, so 3 keeps cost ~halved with little coverage loss.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 5},
    # Terminal structured-output tool. The loop captures its input and stops; NOT dispatched.
    {
        "name": "submit_tldr",
        "description": (
            "Submit the finished daily briefing. Call this exactly ONCE, as your final "
            "action, with the complete TLDR. Do not call any other tool in the same turn. "
            "After you call this, you are done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Short date label, e.g. 'Thu, Jun 4'"},
                "sections": {
                    "type": "array",
                    "description": "The Finance, AI, and Tech sections, in that order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Section name: Finance, AI, or Tech"},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "headline": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                    "required": ["headline", "url"],
                                },
                            },
                        },
                        "required": ["name", "items"],
                    },
                },
            },
            "required": ["date", "sections"],
        },
    },
    # Primary source tools (free).
    _custom_tool("get_finance_news",
        "PRIMARY finance source. Structured recent financial news from Finnhub and Alpha "
        "Vantage; Alpha Vantage items include an AI sentiment label/score you can use to "
        "rank or filter. Use this first for the Finance section, then web_search to fill "
        "gaps or confirm. Returns JSON {headline, url, source, datetime, sentiment?}."),
    _custom_tool("get_tech_news",
        "PRIMARY tech source. Recent headlines from TechCrunch, The Verge, Ars Technica, "
        "and VentureBeat (RSS). Use for the Tech section. Returns JSON "
        "{title, url, source, published}, newest first."),
    _custom_tool("get_ai_news",
        "PRIMARY AI source. Recent AI headlines from AI-specific feeds plus AI-filtered "
        "tech feeds. Use for the AI section. Returns JSON {title, url, source, published}."),
    _custom_tool("get_hacker_news",
        "HN front page (what developers are discussing now). A lead source for Tech/AI; "
        "corroborate important items with the other tools. Returns JSON {title, url, score}."),
]
