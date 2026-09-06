"""Eval runner: run a suite, write a report, diff it against the committed baseline.

    python -m evals.run_evals digest --gate
    python -m evals.run_evals digest --update-baseline

One report shape serves every suite, so the diff/gate logic below is generic. The rule that
makes it generic: everything in `metrics` is a 0-1 float where higher is better. Anything
that isn't on that scale — token counts, item counts, dollars — goes in `counts` and is
never gated. Keep that invariant when adding a suite.

Exit codes:  0 pass · 1 harness error · 2 regression gate failed.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import config
import prompts
from evals.graders import grade_run

REPORTS_DIR = "evals/reports"
BASELINES_DIR = "evals/baselines"
RUNS_DIR = "evals/runs"

# How far a metric may fall before the gate calls it a regression. 2pp absorbs the run-to-run
# wobble of a model-driven suite without hiding a real drop.
DEFAULT_TOLERANCE = 0.02


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - provenance is nice to have, never worth failing a run
        return "unknown"


def _prompt_sha() -> str:
    """Identifies the exact editorial policy a report was produced under. A baseline recorded
    under a different policy may have stale gold labels — the gate warns rather than fails,
    because a deliberate policy change SHOULD move the numbers."""
    return hashlib.sha256(prompts.SYSTEM.encode()).hexdigest()[:12]


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- suites ------------------------------------------------------------------------------


def suite_digest(args) -> dict:
    """Grade recorded run bundles against the pipeline's own rules. Free, offline, keyless.

    A bundle is what `python run.py --save-run` dumps: the raw submission plus the
    provenance, timestamps, memory and caps that were in effect. Replay emits the same shape.
    """
    path = args.runs
    if os.path.isdir(path):
        paths = sorted(os.path.join(path, f) for f in os.listdir(path) if f.endswith(".json"))
    elif os.path.isfile(path):
        paths = [path]
    else:
        raise SystemExit(f"no run bundles at {path!r} — record one with `python run.py --save-run`")
    if not paths:
        raise SystemExit(f"no *.json run bundles in {path!r} — "
                         f"record one with `python run.py --save-run`")
    if args.limit:
        paths = paths[-args.limit:]  # most recent N; bundles are named by date

    per_run, sums, counts = {}, {}, {}
    for p in paths:
        graded = grade_run(_load_json(p))
        per_run[os.path.basename(p)] = graded
        for k, v in graded["metrics"].items():
            sums[k] = sums.get(k, 0.0) + v
        for k, v in graded["counts"].items():
            if isinstance(v, int):
                counts[k] = counts.get(k, 0) + v

    metrics = {k: round(v / len(paths), 4) for k, v in sums.items()}
    # Surface the worst offending check across all runs, so a report reads at a glance.
    failed = sorted({c["name"] for g in per_run.values() for c in g["checks"] if not c["passed"]})
    return {
        "metrics": metrics,
        "counts": {**counts, "bundles": len(paths)},
        "breakdowns": {"by_run": {k: v["metrics"] for k, v in per_run.items()}},
        "checks": per_run[os.path.basename(paths[-1])]["checks"],  # detail from the newest run
        "failures": [{"check": name} for name in failed],
        "dataset": {"path": path, "n": len(paths)},
    }


def suite_routing(args) -> dict:
    """Score prompts.py's section policy against the labeled dataset. Costs a few cents."""
    from evals.routing import run as run_routing
    return run_routing(args)


SUITES = {
    "digest": suite_digest,
    "routing": suite_routing,
}


# --- report ------------------------------------------------------------------------------


def build_report(suite: str, result: dict, started: float, args) -> dict:
    return {
        "schema": 1,
        "suite": suite,
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started)),
        "started_at": started,
        "duration_s": round(time.time() - started, 2),
        "git_sha": _git_sha(),
        "subject": {
            "model": args.model or config.MODEL,
            "prompt_sha": _prompt_sha(),
            "config": {"MAX_STORY_AGE_DAYS": config.MAX_STORY_AGE_DAYS,
                       "MAX_HEADLINES_PER_SECTION": config.MAX_HEADLINES_PER_SECTION},
        },
        "dataset": result.get("dataset", {}),
        "metrics": result.get("metrics", {}),
        "counts": result.get("counts", {}),
        "breakdowns": result.get("breakdowns", {}),
        "confusion": result.get("confusion", {}),
        "checks": result.get("checks", []),
        "failures": result.get("failures", []),
    }


def print_report(report: dict) -> None:
    m, c = report["metrics"], report["counts"]
    print(f"\n=== {report['suite']} — {report['subject']['model']} "
          f"(prompt {report['subject']['prompt_sha']}, git {report['git_sha']}) ===")
    width = max((len(k) for k in m), default=0)
    for name, value in sorted(m.items(), key=lambda kv: (kv[0] != "digest_integrity", kv[0])):
        flag = "" if value >= 0.999 else "  <-"
        print(f"  {name:<{width}}  {value:.3f}{flag}")
    if c:
        print("  " + "  ".join(f"{k}={v}" for k, v in c.items() if isinstance(v, int)))
    for check in report.get("checks", []):
        for ex in check.get("examples", []):
            print(f"    [{check['name']}] {ex.get('why','')}: {ex.get('headline','')}")

    by_section = report.get("breakdowns", {}).get("by_section")
    if by_section:
        print(f"\n  {'section':<16}{'prec':>7}{'recall':>8}{'f1':>7}{'n':>5}")
        for name, s in by_section.items():
            if s["support"]:
                print(f"  {name:<16}{s['precision']:>7.2f}{s['recall']:>8.2f}"
                      f"{s['f1']:>7.2f}{s['support']:>5}")

    by_tag = report.get("breakdowns", {}).get("by_tag")
    if by_tag:
        # Report-only: a bucket of ~5 swings 20 points on one item, so these inform rather
        # than gate. Low-support tags are marked so a moved number isn't over-read.
        print(f"\n  {'tag':<18}{'acc':>6}{'n':>5}")
        for name, t in sorted(by_tag.items(), key=lambda kv: kv[1]["accuracy"]):
            weak = "  (thin — informational)" if t["support"] < 8 else ""
            print(f"  {name:<18}{t['accuracy']:>6.2f}{t['support']:>5}{weak}")

    failures = report.get("failures", [])
    if failures and "gold" in (failures[0] or {}):
        print(f"\n  {len(failures)} disagreements (gold <- predicted):")
        for f in failures[:12]:
            print(f"    {f['gold']:<15} <- {str(f['pred']):<15} {f['headline'][:62]}")
            if f.get("reason"):
                print(f"        model's reason: {f['reason']}")


def compare(report: dict, baseline: dict, tolerance: float) -> list:
    """Regressions only. A metric that IMPROVED is never a failure, and a metric the baseline
    doesn't know about is new information, not a problem. A metric the baseline HAS but the
    report lost is a failure: it means a suite quietly stopped measuring something.
    """
    out = []
    for name, was in baseline.get("metrics", {}).items():
        now = report["metrics"].get(name)
        if now is None:
            out.append((name, was, None, "metric disappeared"))
        elif now < was - tolerance:
            out.append((name, was, now, f"-{was - now:.3f}"))
    return out


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m evals.run_evals",
                                description="Run an eval suite and gate it against a baseline.")
    p.add_argument("suites", nargs="+", choices=[*SUITES, "all"])
    p.add_argument("--model", default=None, help="override the model under test")
    p.add_argument("--runs", default=RUNS_DIR, help="digest: run-bundle dir or single file")
    p.add_argument("--dataset", default="evals/datasets/routing.jsonl", help="routing: label set")
    p.add_argument("--require-human", action=argparse.BooleanOptionalAction, default=True,
                   help="routing: score only rows you reviewed (default: yes). Turning this "
                        "off grades against bootstrap guesses and measures very little.")
    p.add_argument("--no-cache", action="store_true", help="routing: ignore cached verdicts")
    p.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    p.add_argument("--limit", type=int, default=None, help="use only the N most recent inputs")
    p.add_argument("--out", default=None, help=f"report path (default {REPORTS_DIR}/<id>-<suite>.json)")
    p.add_argument("--baseline", default=None, help=f"baseline path (default {BASELINES_DIR}/<suite>.json)")
    p.add_argument("--gate", action="store_true", help="exit 2 if any metric regressed")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    p.add_argument("--update-baseline", action="store_true", help="accept this report as the baseline")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    suites = list(SUITES) if "all" in args.suites else args.suites
    regressed = False

    for suite in suites:
        started = time.time()
        report = build_report(suite, SUITES[suite](args), started, args)

        os.makedirs(REPORTS_DIR, exist_ok=True)
        out = args.out or os.path.join(REPORTS_DIR, f"{report['run_id']}-{suite}.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)

        if not args.quiet:
            print_report(report)
            print(f"  -> {out}")

        baseline_path = args.baseline or os.path.join(BASELINES_DIR, f"{suite}.json")

        if args.update_baseline:
            os.makedirs(BASELINES_DIR, exist_ok=True)
            with open(baseline_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
            print(f"  baseline updated: {baseline_path}")
            continue

        if not os.path.exists(baseline_path):
            print(f"  no baseline yet ({baseline_path}); record one with --update-baseline")
            continue

        baseline = _load_json(baseline_path)
        if baseline.get("subject", {}).get("prompt_sha") != report["subject"]["prompt_sha"]:
            print("  WARNING: prompts.py changed since this baseline was recorded. Gold labels "
                  "are derived from the policy, so some may need re-review.")

        drops = compare(report, baseline, args.tolerance)
        for name, was, now, why in drops:
            shown = "missing" if now is None else f"{now:.3f}"
            print(f"  REGRESSION {name}: {was:.3f} -> {shown} ({why})")
        if drops:
            regressed = True
        elif not args.quiet:
            print(f"  gate: OK vs {baseline_path}")

    if regressed and args.gate:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
