"""Tool definitions for the agent.

Two kinds of tools live here:

1. **Server tools** (`web_search`, `web_fetch`) — Anthropic executes these on their
   side, inside a single API turn. We never run them ourselves; we only declare them.
2. **Custom client tools** (`get_hacker_news`) — we run these. The agent emits a
   `tool_use` block, our loop calls `dispatch()`, and we feed the result back.

`TOOLS` is the JSON list handed to the Messages API. `dispatch()` maps a custom tool
name to the Python function that implements it.
"""

import json

import requests

HN_BASE = "https://hacker-news.firebaseio.com/v0"
HTTP_TIMEOUT = 10


# --- Custom client tool implementations -------------------------------------

def get_hacker_news(limit: int = 15) -> str:
    """Return the current Hacker News top stories as a compact JSON string.

    Raises on network/HTTP errors; the loop's dispatch() turns that into an
    error tool_result so the agent can route around it rather than crashing.
    """
    limit = max(1, min(int(limit), 30))
    ids = requests.get(f"{HN_BASE}/topstories.json", timeout=HTTP_TIMEOUT).json()[:limit]
    stories = []
    for story_id in ids:
        item = requests.get(f"{HN_BASE}/item/{story_id}.json", timeout=HTTP_TIMEOUT).json()
        if not item or item.get("type") != "story":
            continue
        stories.append(
            {
                "title": item.get("title"),
                "url": item.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                "score": item.get("score", 0),
            }
        )
    return json.dumps(stories)


# Registry of custom (client-executed) tools: name -> callable(**input) -> str
CLIENT_TOOLS = {
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


# --- Tool declarations passed to the API ------------------------------------

TOOLS = [
    # Server tool: Anthropic runs the search loop (capped by max_uses). $10/1k searches.
    # Lower cap = fewer results pulled into context = lower token cost per run.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    # Server tool: read a specific page the agent decides is worth investigating.
    # Can only fetch URLs already seen in context (built-in exfiltration guardrail).
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 3},
    # Terminal "structured output" tool. The agent calls this once, as its final action,
    # to hand back the finished briefing. The API enforces this schema, so we never have to
    # parse free-form text — the agent's prose reasoning can't corrupt the output. The loop
    # captures the tool input and stops; it is NOT dispatched like a normal tool.
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
    # Custom client tool: seed tech coverage + exercise the hand-written tool loop.
    {
        "name": "get_hacker_news",
        "description": (
            "Fetch the current top stories from Hacker News (tech/startup/programming "
            "community front page). Use this to seed the technology section with stories "
            "the community is discussing right now, then corroborate or expand on the "
            "important ones with web_search. Returns a JSON list of "
            "{title, url, score} ordered by HN ranking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "How many top stories to return (1-30, default 15).",
                }
            },
            "required": [],
        },
    },
]
