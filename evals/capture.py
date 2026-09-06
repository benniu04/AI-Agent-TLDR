"""Freeze a day's candidate pool: call every client tool for real, store the results verbatim.

Two jobs, one artifact:
  - the raw material for labeling (evals/label.py reads the flattened items), and
  - the fixture the replay eval feeds to the agent through run_agent's `dispatch_fn`, so the
    model does real editorial work on inputs that don't change between runs.

Storing the result STRING untouched is the point — the replay dispatcher hands the model
exactly the bytes the live tool would have, so a replay differs from production only in where
the bytes came from. Anything reshaped here would be a difference the eval can't see.

Only get_finance_news needs credentials (Finnhub / Alpha Vantage); the other five are RSS and
Algolia. A tool that fails is recorded as a failure rather than aborting the capture — a pool
missing one source is still worth labeling.

    python -m evals.capture                    # today, 40 items per tool
    python -m evals.capture --date 2026-09-02 --limit 60
"""

import argparse
import json
import os
import time

import config
import memory
import tools

FIXTURES_DIR = "evals/fixtures"

# Captured wider than production asks for (the brief's tools default to 20) so the pool holds
# the also-rans — the items the model DIDN'T pick. Those are most of the DROP class, and
# without them a labeled dataset only ever sees the model's own choices.
DEFAULT_LIMIT = 40


def capture(date: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Call every client tool once and return the pool bundle (does not write it)."""
    pool = {}
    for name in tools.CLIENT_TOOLS:
        started = time.time()
        content, is_error = tools.dispatch(name, {"limit": limit})
        pool[name] = {"content": content, "is_error": is_error,
                      "duration_s": round(time.time() - started, 2)}
        status = "ERROR" if is_error else f"{len(json.loads(content) if not is_error else [])} items"
        print(f"  {name:<20} {status}")
        if is_error:
            print(f"    {content[:120]}")

    return {
        "schema": 1,
        "date": date,
        "captured_at": time.time(),
        "limit": limit,
        "tools": pool,
        # Frozen alongside the pool because the replay eval must use THIS memory, not live
        # memory/seen.json — CI commits new memory every weekday, which would drift replay
        # scores for reasons that have nothing to do with the prompt.
        "memory_snapshot": memory.load_entries(config.MEMORY_PATH),
        "config": {"MAX_STORY_AGE_DAYS": config.MAX_STORY_AGE_DAYS,
                   "MAX_HEADLINES_PER_SECTION": config.MAX_HEADLINES_PER_SECTION,
                   "MAX_HEADLINES_MONEY_MOVEMENT": config.MAX_HEADLINES_MONEY_MOVEMENT,
                   "MAX_HEADLINES_LIQUIDITY": config.MAX_HEADLINES_LIQUIDITY},
    }


def iter_items(bundle: dict):
    """Yield normalized {headline, url, source, ts, tool} from a pool bundle.

    The six tools disagree on key names — Alpha Vantage says `headline`/`datetime`, the RSS
    feeds say `title`/`published`/`ts`, Hacker News says `title`/`score`. Normalizing here
    keeps that mess out of the dataset, and out of the labeling UI.
    """
    for tool_name, entry in (bundle.get("tools") or {}).items():
        if entry.get("is_error"):
            continue
        try:
            items = json.loads(entry["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            headline = it.get("headline") or it.get("title")
            url = it.get("url")
            if not headline or not url:
                continue
            yield {
                "headline": str(headline).strip(),
                "url": str(url).strip(),
                "source": it.get("source") or tool_name.replace("get_", "").replace("_news", ""),
                "ts": it.get("ts") or it.get("datetime") or it.get("published"),
                "tool": tool_name,
            }


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m evals.capture",
                                description="Freeze a day's candidate pool for labeling and replay.")
    p.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="items per tool")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    print(f"Capturing pool for {args.date} ({args.limit}/tool)...")
    bundle = capture(args.date, args.limit)

    out = args.out or os.path.join(FIXTURES_DIR, args.date, "pool.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)

    items = list(iter_items(bundle))
    ok = sum(1 for e in bundle["tools"].values() if not e["is_error"])
    print(f"\n{len(items)} items from {ok}/{len(bundle['tools'])} tools -> {out}")
    print(f"Next: python -m evals.label bootstrap --pool {args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
