"""Build and review the routing dataset — the gold labels the routing eval scores against.

    python -m evals.capture                          # 1. get candidates (free, ~no keys)
    python -m evals.label bootstrap --pool 2026-09-03 # 2. seed rows with a provisional label
    python -m evals.label review                      # 3. the part only you can do
    python -m evals.label stats                       # 4. see what the set still needs

WHY A HUMAN HAS TO DO STEP 3
`bootstrap` guesses each item's section from the tool that returned it. That guess is a weak
prior and frequently wrong — the payments feed surfaces AI stories, the liquidity feed
surfaces equity stories that prompts.py explicitly excludes. Those wrong guesses are the most
valuable rows in the set, because they're exactly the calls the editorial policy exists to
make. If the guesses were trusted as gold, the eval would be scoring the feeds' opinion
instead of yours, and would happily confirm a prompt that routes everything wrong.

So only rows you have confirmed count: the routing suite scores `reviewed: true` rows and
ignores the rest. There is no way to accidentally grade against a machine's guess.
"""

import argparse
import json
import os
import re
import sys
import time

import config
import memory
from evals.capture import FIXTURES_DIR, iter_items
from evals.labels import DROP, SECTIONS

DATASET = "evals/datasets/routing.jsonl"

# The tool that returned an item -> the section it PROBABLY belongs to. A starting point for
# the reviewer's cursor, never a label: see this module's docstring.
_TOOL_PRIOR = {
    "get_finance_news": "finance",
    "get_payments_news": "money_movement",
    "get_liquidity_news": "liquidity",
    "get_ai_news": "ai",
    "get_tech_news": "tech",
    "get_hacker_news": "tech",
}

# Tags mark the buckets prompts.py actually argues about, so a regression report can say
# WHICH kind of judgment moved rather than just "accuracy fell". Auto-applied at bootstrap
# (cheap and roughly right); editable in review with `g`.
_TAG_RULES = {
    "cross-border": r"\bcross-border\b|\bremittanc|\bcorridor\b",
    "cbdc": r"\bcbdc\b|digital euro|digital yuan|central bank digital",
    "stablecoin": r"\bstablecoin|\btokeni[sz]ed deposit|\bgenius act\b",
    "p2p": r"\bzelle\b|\bvenmo\b|cash app|early warning|\bp2p\b",
    "rails": r"\bfednow\b|\brtp\b|the clearing house|instant payment|real-time payment",
    "card-network": r"\bvisa\b|\bmastercard\b|interchange|\bswipe fee",
    "fraud": r"\bfraud|\bscam|\bphishing|reimburs",
    "commodity": r"\bgold\b|\boil\b|\bcrude\b|\bbrent\b|commodit",
    "rate-reaction": r"\byield|\btreasur|rate cut|rate hike|\bfed\b|\bfomc\b|basis point",
    "bank-earnings": r"\bearnings\b|\bq[1-4] (results|profit)|beats estimates",
    "token-price": r"\bbitcoin\b|\bether|\bcrypto\b.*\b(price|rally|surge|plunge)",
    "breach": r"\bbreach\b|\bhack(ed|ers?)?\b|ransomware|data leak|\bcyber",
    "bnpl": r"\bbnpl\b|buy now,? pay later|\bklarna\b|\baffirm\b",
    "vc-round": r"\braises \$|\bseries [a-e]\b|\bfunding round\b|\bvaluation\b",
    "foreign-neobank": r"\bfca\b|\bneobank\b|\brevolut\b|\bmonzo\b|\bnubank\b|\bupi\b",
    "corporate-spend": r"\bexpense\b|\bramp\b|\bairwallex\b|corporate card|\bb2b\b",
    "opinion": r"^why |\bshould\b.*\?$|\bopinion\b|\bcommentary\b|\bhere's what\b",
}

_KEYS = {"f": "finance", "m": "money_movement", "l": "liquidity",
         "a": "ai", "t": "tech", "d": DROP}


def _canon(url: str) -> str:
    return memory.canonical_url(url)


def _auto_tags(headline: str) -> list:
    text = headline.lower()
    return sorted(t for t, pat in _TAG_RULES.items() if re.search(pat, text))


def load_dataset(path: str = DATASET) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def save_dataset(rows: list, path: str = DATASET) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"  # atomic-ish: a Ctrl-C mid-write must not shred a labeled dataset
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# --- bootstrap ----------------------------------------------------------------------------


def cmd_bootstrap(args) -> int:
    rows = load_dataset(args.dataset)
    known = {_canon(r["url"]) for r in rows}
    next_id = max((int(r["id"][1:]) for r in rows if r["id"][1:].isdigit()), default=0) + 1

    pools = args.pool or sorted(os.listdir(FIXTURES_DIR)) if os.path.isdir(FIXTURES_DIR) else []
    if isinstance(pools, str):
        pools = [pools]

    candidates = []
    for date in pools:
        path = os.path.join(FIXTURES_DIR, date, "pool.json")
        if not os.path.exists(path):
            print(f"  no pool at {path}, skipping")
            continue
        with open(path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        got = list(iter_items(bundle))
        print(f"  {date}: {len(got)} items")
        candidates.extend(got)

    # Items we actually delivered are worth labeling too — they're the ones that passed every
    # filter, so they're the set most likely to expose a routing call that LOOKED fine.
    if args.include_memory:
        for e in memory.load_entries(config.MEMORY_PATH):
            if e.get("url") and e.get("headline"):
                candidates.append({"headline": e["headline"], "url": e["url"],
                                   "source": "delivered", "ts": e.get("ts"),
                                   "tool": "memory"})
        print(f"  memory/seen.json: {len(memory.load_entries(config.MEMORY_PATH))} delivered items")

    added = 0
    for c in candidates:
        key = _canon(c["url"])
        if key in known:
            continue  # never re-add, and never clobber a row you've already reviewed
        known.add(key)
        rows.append({
            "id": f"r{next_id:04d}",
            "headline": c["headline"],
            "url": c["url"],
            "source": c["source"],
            "ts": c["ts"],
            "tool": c["tool"],
            "gold_section": _TOOL_PRIOR.get(c["tool"]),  # a guess, not a label
            "tags": _auto_tags(c["headline"]),
            "reviewed": False,
            "label_source": "bootstrap",
            "labeled_at": None,
            "notes": "",
        })
        next_id += 1
        added += 1

    save_dataset(rows, args.dataset)
    print(f"\n+{added} new rows ({len(rows)} total, "
          f"{sum(1 for r in rows if r['reviewed'])} reviewed) -> {args.dataset}")
    print("Next: python -m evals.label review")
    return 0


# --- review -------------------------------------------------------------------------------


def _getch() -> str:
    """One keypress, no Enter. Falls back to line input where that isn't possible (piped
    stdin, Windows), so the tool still works — just one Enter slower."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # noqa: BLE001 - not a tty, or no termios; degrade rather than fail
        return (sys.stdin.readline() or "q").strip()[:1] or "s"


def _age(ts) -> str:
    try:
        hours = (time.time() - float(ts)) / 3600
    except (TypeError, ValueError):
        return "unknown age"
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


_HELP = ("  [f]inance  [m]oney-movement  [l]iquidity  [a]i  [t]ech  [d]rop"
         "   ·   [s]kip  [b]ack  [g] tags  [q] save+quit")

_HELP_DISPUTED = ("  [k] keep mine   ·   [f]inance [m]oney-movement [l]iquidity [a]i [t]ech "
                  "[d]rop   ·   [s]kip [b]ack [q]uit")


def _load_disputes(path: str | None) -> tuple[dict, str]:
    """Read a routing report's disagreements into {row_id: {pred, reason}}.

    A routing run is, incidentally, a free audit of the dataset: every disagreement is either
    a model error or a label error, and the model's stated reason usually tells you which
    within a couple of seconds. Reviewing that list is far higher-yield per minute than
    labeling fresh items, because each row is already known to be contested.
    """
    import glob
    if not path or path == "latest":
        found = sorted(glob.glob(os.path.join("evals/reports", "*-routing.json")))
        if not found:
            raise SystemExit("no routing report found — run "
                             "`python -m evals.run_evals routing` first")
        path = found[-1]
    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)
    disputes = {f["id"]: {"pred": f.get("pred"), "reason": f.get("reason", "")}
                for f in report.get("failures", []) if "id" in f}
    return disputes, f"{report['subject']['model']} ({os.path.basename(path)})"


def cmd_review(args) -> int:
    rows = load_dataset(args.dataset)
    if not rows:
        print(f"No dataset at {args.dataset}. Run `python -m evals.label bootstrap` first.")
        return 1

    disputes, judged_by = ({}, "")
    if args.disputed:
        disputes, judged_by = _load_disputes(args.disputed)

    queue = [r for r in rows
             if (args.all or not r["reviewed"] or r["id"] in disputes)
             and (not disputes or r["id"] in disputes)
             and (not args.tag or args.tag in r["tags"])
             and (not args.tool or r["tool"] == args.tool)]
    if args.limit:
        queue = queue[:args.limit]
    if not queue:
        print("Nothing matches — everything here is already reviewed.")
        return 0

    if disputes:
        print(f"\nAuditing {len(queue)} rows where {judged_by} disagreed with you.\n"
              f"Each one is either a model error or a label error — the model's reason "
              f"usually tells you which.\n{_HELP_DISPUTED}\n")
    else:
        print(f"\nReviewing {len(queue)} items. Your call is the gold label; the suggestion is "
              f"just where the cursor starts.\n{_HELP}\n")

    i, changed, kept = 0, 0, 0
    while i < len(queue):
        r = queue[i]
        dispute = disputes.get(r["id"])
        print(f"\n[{i + 1}/{len(queue)}]  {r['tool'].replace('get_', '')} · {r['source']} · {_age(r['ts'])}")
        print(f"  \033[1m{r['headline'][:110]}\033[0m")
        print(f"  {r['url'][:110]}")
        if dispute:
            print(f"  you: \033[1m{r['gold_section']}\033[0m"
                  f"    model said: \033[1m{dispute['pred']}\033[0m")
            if dispute["reason"]:
                print(f"  its reason: {dispute['reason']}")
        else:
            print(f"  suggested: {r['gold_section'] or '?'}    tags: {', '.join(r['tags']) or '—'}")
        sys.stdout.write("  > ")
        sys.stdout.flush()

        key = _getch().lower()
        print()

        if key == "q":
            break
        if key == "s":
            i += 1
            continue
        if key == "k":
            # Re-confirming under challenge is a real signal, so stamp it: this label has now
            # survived a model arguing the other way.
            r["labeled_at"] = time.strftime("%Y-%m-%d")
            r["notes"] = (r.get("notes") or "") or f"upheld vs {judged_by}"
            kept += 1
            save_dataset(rows, args.dataset)
            i += 1
            continue
        if key == "b":
            i = max(0, i - 1)
            continue
        if key == "g":
            raw = input("  tags (comma-separated): ").strip()
            r["tags"] = sorted({t.strip() for t in raw.split(",") if t.strip()})
            save_dataset(rows, args.dataset)
            continue
        if key not in _KEYS:
            print(_HELP_DISPUTED if disputes else _HELP)
            continue

        if dispute and _KEYS[key] != r["gold_section"]:
            r["notes"] = f"was {r['gold_section']}; corrected during audit vs {judged_by}"
        r["gold_section"] = _KEYS[key]
        r["reviewed"] = True
        r["label_source"] = "human"
        r["labeled_at"] = time.strftime("%Y-%m-%d")
        changed += 1
        save_dataset(rows, args.dataset)  # after every decision — a Ctrl-C loses nothing
        i += 1

    save_dataset(rows, args.dataset)
    reviewed = sum(1 for r in rows if r["reviewed"])
    if disputes:
        print(f"\nAudit: {changed} label(s) corrected, {kept} upheld. "
              f"Re-run the routing eval to see the corrected score.")
    print(f"Labeled {changed} this session · {reviewed}/{len(rows)} reviewed overall.")
    return 0


# --- stats --------------------------------------------------------------------------------


def cmd_stats(args) -> int:
    rows = load_dataset(args.dataset)
    if not rows:
        print(f"No dataset at {args.dataset}.")
        return 1
    reviewed = [r for r in rows if r["reviewed"]]
    print(f"\n{len(rows)} rows · {len(reviewed)} reviewed (only these are scored)\n")

    print(f"  {'section':<16}{'reviewed':>9}{'unreviewed':>12}")
    for section in (*SECTIONS, DROP):
        n_ok = sum(1 for r in reviewed if r["gold_section"] == section)
        n_no = sum(1 for r in rows if not r["reviewed"] and r["gold_section"] == section)
        flag = "   <- thin" if n_ok < args.target else ""
        print(f"  {section:<16}{n_ok:>9}{n_no:>12}{flag}")

    # Delivered items carry no source tool, so bootstrap has no prior for them. They're not
    # missing — they're the rows with no cursor position, and reviewing them is pure signal.
    unassigned = sum(1 for r in rows if not r["reviewed"] and r["gold_section"] is None)
    if unassigned:
        print(f"  {'(no prior)':<16}{'':>9}{unassigned:>12}   <- delivered items, no feed to guess from")

    print(f"\n  {'tag':<18}{'reviewed':>9}")
    tags = {t for r in rows for t in r["tags"]}
    for tag in sorted(tags):
        n = sum(1 for r in reviewed if tag in r["tags"])
        print(f"  {tag:<18}{n:>9}{'   <- thin' if n < 8 else ''}")

    # The reviewer disagreeing with the feed is the signal that labeling was worth doing.
    flipped = [r for r in reviewed if r["gold_section"] != _TOOL_PRIOR.get(r["tool"])]
    if reviewed:
        print(f"\n  {len(flipped)}/{len(reviewed)} reviewed rows disagree with their source feed "
              f"({len(flipped) / len(reviewed):.0%}) — those are the rows the eval earns its keep on.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m evals.label",
                                description="Build and review the routing gold labels.")
    p.add_argument("--dataset", default=DATASET)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="seed rows from captured pools (+ delivered memory)")
    b.add_argument("--pool", action="append", help="fixture date; repeatable. Default: all")
    b.add_argument("--include-memory", action="store_true", default=True)
    b.add_argument("--no-include-memory", dest="include_memory", action="store_false")
    b.set_defaults(func=cmd_bootstrap)

    r = sub.add_parser("review", help="interactively confirm each item's section")
    r.add_argument("--tag", help="only items carrying this tag")
    r.add_argument("--tool", help="only items from this source tool")
    r.add_argument("--limit", type=int)
    r.add_argument("--all", action="store_true", help="include already-reviewed rows")
    r.add_argument("--disputed", nargs="?", const="latest", default=None,
                   metavar="REPORT",
                   help="audit only the rows a routing run disagreed with, showing its "
                        "reasoning (default: the newest routing report)")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("stats", help="class + tag coverage")
    s.add_argument("--target", type=int, default=15, help="per-section target")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
