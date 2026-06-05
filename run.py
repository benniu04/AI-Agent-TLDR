"""Entrypoint: build the goal, run the agent, format, and deliver.

Order matters: deliver only AFTER the agent has fully finished. The send is the harness's
job, not the agent's. The agent returns JSON; we format it for the chosen channel.

Flags:
  --dry-run   run the agent and print the formatted output, but do NOT deliver.

Channel is set by the DELIVERY env var: "sms" (Twilio) or "telegram".
"""

import sys

import config
from agent import run_agent
from deliver import send_telegram
from deliver_sms import send_sms
from formatting import delivered_count, format_sms, format_telegram, parse_agent_json
from prompts import SYSTEM, build_goal


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    result = run_agent(goal=build_goal(), system=SYSTEM)
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

    cap = config.MAX_HEADLINES_PER_SECTION
    allowed = result.seen_urls  # provenance allowlist (fail-open if empty)
    if config.DELIVERY == "telegram":
        message = format_telegram(data, cap, allowed)
        sender = send_telegram
    else:  # default: sms
        message = format_sms(data, cap, allowed)
        sender = send_sms

    # Report how many items the filters dropped (banned/mismatch/provenance/dup).
    submitted = sum(len(s.get("items", [])) for s in data.get("sections", []))
    delivered = delivered_count(data, cap, allowed)
    print(f"--- items: {submitted} submitted, {delivered} delivered, "
          f"{submitted - delivered} dropped | {len(allowed)} result URLs seen ---")
    print(f"\n--- formatted for {config.DELIVERY} ({len(message)} chars) ---\n{message}")

    if result.stop in ("max_iterations", "unknown"):
        print(f"\nWARNING: agent stopped on '{result.stop}'; delivering anyway.", file=sys.stderr)

    if dry_run:
        print(f"\n(--dry-run: skipping {config.DELIVERY} send)")
        return 0

    sender(message)
    print(f"\nDelivered via {config.DELIVERY}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
