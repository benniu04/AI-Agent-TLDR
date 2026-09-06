"""Entrypoint: build the goal, run the agent, format, and deliver to Telegram.

Order matters: deliver only AFTER the agent has fully finished. The send is the harness's
job, not the agent's. The agent returns JSON; we format it as a Telegram message.

Flags:
  --dry-run    run the agent and print the formatted output, but do NOT deliver.
  --save-run   also dump a run bundle to evals/runs/ for the digest eval to grade later.
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import memory
from agent import run_agent
from deliver import send_telegram
from formatting import delivered_count, delivered_items, format_telegram, parse_agent_json
from prompts import SYSTEM, build_goal


RUNS_DIR = "evals/runs"


def _save_run_bundle(now, data, result, allowed, repeats, cap, section_caps) -> str:
    """Record everything evals/graders.py needs to grade this run after the fact.

    The graders score the RAW submission, not the delivered digest — the filter chain drops
    every violation before delivery, so by then there is nothing left to measure. That means
    the bundle has to carry the context the filters used (provenance, publish times, memory,
    caps) alongside the untouched `data`, or the grades can't be reproduced.

    Written by the production run, so each weekday's real digest becomes a graded sample for
    free. Deliberately stdlib-only and best-effort: an eval artifact must never be able to
    break the delivery it's observing.
    """
    os.makedirs(RUNS_DIR, exist_ok=True)
    path = os.path.join(RUNS_DIR, f"{now.strftime('%Y-%m-%d')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "schema": 1,
            "run_id": os.path.basename(result.log_path),
            "now_ts": now.timestamp(),
            "model": config.MODEL,
            "stop": result.stop,
            "data": data,
            "seen_urls": sorted(allowed),
            "url_ts": result.url_ts,
            "repeat_urls": sorted(repeats),
            "caps": {"default": cap, **section_caps},
        }, fh, indent=2)
    return path


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    save_run = "--save-run" in sys.argv

    # One timestamp in the user's timezone, used both to anchor the agent's recency and to
    # stamp the display label — so the model never has to guess the date.
    now = datetime.now(ZoneInfo(config.TIMEZONE))

    # Cross-run memory: what we've already delivered recently. Used as a soft signal in the
    # goal (skip repeats) and a hard filter below (drop URLs we've already sent).
    mem_entries = memory.load_entries(config.MEMORY_PATH)
    mem_cutoff = now.timestamp() - config.MEMORY_KEEP_DAYS * 86400
    recent = memory.recent_headlines(mem_entries, mem_cutoff)
    repeats = memory.seen_url_set(mem_entries)

    result = run_agent(goal=build_goal(now.strftime("%A, %B %-d, %Y"), recent=recent), system=SYSTEM)
    print(f"--- agent done: stop={result.stop} iters={result.iterations} "
          f"tokens={result.tokens} log={result.log_path} ---")

    # Preferred path: the agent submitted structured data via the submit_tldr tool.
    data = result.data
    if data is None:
        # Fallback: parse JSON from the final text (older behavior / unexpected stop).
        if not result.text:
            print("ERROR: agent neither submitted nor produced text; not delivering.",
                  file=sys.stderr)
            return 1
        try:
            data = parse_agent_json(result.text)
        except ValueError as exc:
            print(f"ERROR: agent did not submit and text isn't parseable ({exc}). Raw:\n"
                  f"{result.text}", file=sys.stderr)
            return 1

    # Stamp the display date ourselves (same timestamp used to anchor the agent above).
    data["date"] = now.strftime("%a, %b %-d")

    cap = config.MAX_HEADLINES_PER_SECTION
    # Per-section cap overrides (the priority finance beats get higher ceilings).
    section_caps = {"money movement": config.MAX_HEADLINES_MONEY_MOVEMENT,
                    "liquidity": config.MAX_HEADLINES_LIQUIDITY}
    allowed = result.seen_urls  # provenance allowlist (fail-open if empty)
    # Deterministic recency backstop: URLs whose known publish time is older than the cutoff.
    cutoff = now.timestamp() - config.MAX_STORY_AGE_DAYS * 86400
    stale = {u for u, ts in result.url_ts.items() if ts and ts < cutoff}

    message = format_telegram(data, cap, allowed, stale, repeats, section_caps)

    # Report how many items the filters dropped (banned/mismatch/provenance/dup/stale/repeat).
    submitted = sum(len(s.get("items") or []) for s in data.get("sections") or [])
    delivered = delivered_count(data, cap, allowed, stale, repeats, section_caps)
    repeats_hit = sum(1 for s in data.get("sections") or [] for it in s.get("items") or []
                      if it.get("url") and memory.canonical_url(it["url"]) in repeats)
    print(f"--- items: {submitted} submitted, {delivered} delivered, "
          f"{submitted - delivered} dropped | {len(allowed)} URLs seen, "
          f"{len(stale)} stale (>{config.MAX_STORY_AGE_DAYS}d), "
          f"{repeats_hit} repeats (memory: {len(repeats)} URLs) ---")
    print(f"\n--- formatted for telegram ({len(message)} chars) ---\n{message}")

    if save_run:
        try:
            print(f"--- run bundle: {_save_run_bundle(now, data, result, allowed, repeats, cap, section_caps)} ---")
        except OSError as exc:  # never let an eval artifact block the digest
            print(f"WARNING: could not write run bundle ({exc})", file=sys.stderr)

    if result.stop in ("max_iterations", "unknown"):
        print(f"\nWARNING: agent stopped on '{result.stop}'; delivering anyway.", file=sys.stderr)

    if dry_run:
        print("\n(--dry-run: skipping Telegram send and memory update)")
        return 0

    send_telegram(message)
    print("\nDelivered via Telegram.")

    # Record what we delivered so future runs don't repeat it (only after a real send).
    shown = delivered_items(data, cap, allowed, stale, repeats, section_caps)
    kept = memory.update(config.MEMORY_PATH, shown, now.strftime("%Y-%m-%d"),
                         now.timestamp(), config.MEMORY_KEEP_DAYS)
    print(f"Memory: recorded {len(shown)} delivered, {kept} total in "
          f"{config.MEMORY_PATH} (last {config.MEMORY_KEEP_DAYS}d).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
