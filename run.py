"""Entrypoint: build the goal, run the agent, deliver the result.

Order matters: deliver only AFTER the agent has fully finished. The send is the harness's
job, not the agent's.

Flags:
  --dry-run   run the agent and print the TLDR, but do NOT send to Telegram.
"""

import sys

from agent import run_agent
from deliver import send_telegram
from prompts import SYSTEM, build_goal


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    result = run_agent(goal=build_goal(), system=SYSTEM)

    print(f"--- agent done: stop={result.stop} iters={result.iterations} "
          f"tokens={result.tokens} log={result.log_path} ---")
    print(result.text or "(no text produced)")

    if not result.text:
        print("ERROR: agent produced no text; not delivering.", file=sys.stderr)
        return 1

    # Only block delivery on hard failure modes; end_turn/budget/timeout that still
    # yielded a usable briefing are fine to send.
    if result.stop in ("max_iterations", "unknown"):
        print(f"WARNING: agent stopped on '{result.stop}'; delivering anyway.", file=sys.stderr)

    if dry_run:
        print("\n(--dry-run: skipping Telegram send)")
        return 0

    send_telegram(result.text)
    print("\nDelivered to Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
