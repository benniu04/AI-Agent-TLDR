"""The routing eval: does prompts.py put each story in the right section?

This is the regression suite for the editorial policy. It takes the labeled dataset, asks the
model under test where each item belongs, and scores the answer against your call.

TWO DESIGN CHOICES THAT MATTER

1. `prompts.SYSTEM` is sent VERBATIM as the system prompt. Not a paraphrase, not the
   ownership block sliced out — the actual artifact under test. A second copy of the policy
   would drift from the real one, and slicing would hide the STRONGLY PREFER / DEPRIORITIZE /
   EXCLUDE tiers, which are the part every recent commit has touched. Editing prompts.py
   moves this number immediately, which is the whole point.

2. Determinism comes from a prediction cache, not `temperature=0`. Sampling params are
   rejected outright on current models (Opus 5, Sonnet 5, Opus 4.7+), so pinning determinism
   to temperature would block ever sweeping a newer model. The cache keys on the prompt hash,
   so editing prompts.py invalidates every entry — exactly the right invalidation — while an
   unchanged re-run is byte-identical and free.

Grading is pure label comparison. There is no LLM judge here and there doesn't need to be:
you already decided the right answer.
"""

import hashlib
import json
import os
import sys
import time

import anthropic

import config
import prompts
from evals.labels import DROP, LABELS, SECTIONS

CACHE_DIR = "evals/.cache"

# Items per request. Big enough that the cached policy prefix is amortized, small enough that
# one malformed response doesn't cost the whole run.
BATCH_SIZE = 20

# Rough $/1M tokens, for the pre-flight cost estimate only.
_PRICING = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0),
            "claude-sonnet-5": (2.0, 10.0), "claude-opus-5": (5.0, 25.0)}

# The eval-only framing. The load-bearing sentence is the one about recency and dedup: the
# policy makes both hard requirements, so without this the model dutifully drops undated
# dataset items as stale and the eval measures date-guessing instead of section judgment.
_PREAMBLE = """\
You are classifying CANDIDATE items against the editorial policy above — not assembling a
briefing. For each numbered item, decide the single section it belongs in, or DROP if the
policy says to exclude it entirely.

Valid sections: finance | money_movement | liquidity | ai | tech | drop

Judge each item independently, on subject matter only. IGNORE recency and IGNORE duplication:
these items come from different days and have no reliable timestamps, so "is it recent" and
"did we already run it" are not the questions here. Answer only: under the SECTION OWNERSHIP
precedence and the BEAT PRIORITIES tiers, where does this belong?

Use `drop` when the policy excludes the story from the briefing altogether — an EXCLUDE-tier
item, an opinion/think-piece, a roundup or aggregator page, or something simply off-beat for
all five sections.

Call `classify_items` exactly once with a verdict for every item.

ITEMS:
"""

CLASSIFY_TOOL = {
    "name": "classify_items",
    "description": "Return one section verdict per candidate item.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["classifications"],
        "properties": {"classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "section", "reason"],
                "properties": {
                    "id": {"type": "string", "description": "The item's id, verbatim."},
                    "section": {"type": "string", "enum": list(LABELS)},
                    "reason": {"type": "string",
                               "description": "At most 12 words: the policy rule you applied."},
                },
            },
        }},
    },
}


def _prompt_sha() -> str:
    return hashlib.sha256(prompts.SYSTEM.encode()).hexdigest()[:12]


def _cache_key(model: str, row: dict) -> str:
    raw = f"{model}|{_prompt_sha()}|{row['id']}|{row['headline']}|{row['url']}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_put(key: str, value: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{key}.json"), "w", encoding="utf-8") as fh:
        json.dump(value, fh)


def estimate_cost(n_items: int, model: str) -> float:
    """Pre-flight estimate. The policy prefix (~2.6k tokens) is cached after the first batch,
    so the marginal cost per batch is mostly the items and the verdicts."""
    batches = max(1, (n_items + BATCH_SIZE - 1) // BATCH_SIZE)
    in_tok = 2600 + batches * (260 + n_items / batches * 45)
    out_tok = n_items * 30
    price_in, price_out = _PRICING.get(model, (3.0, 15.0))
    return (in_tok * price_in + out_tok * price_out) / 1_000_000


def _classify_batch(client, model: str, rows: list) -> dict:
    """One request; returns {id: {"section", "reason"}}."""
    listing = "\n".join(
        f"{r['id']}. {r['headline']}\n   source: {r['source']} | url: {r['url']}" for r in rows)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        # The cache breakpoint sits on the policy, which is identical across every batch — so
        # batch 2 onward reads it instead of paying for it.
        system=[{"type": "text", "text": prompts.SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_items"},
        messages=[{"role": "user", "content": _PREAMBLE + listing}],
    )
    out = {}
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_items":
            for c in block.input.get("classifications", []):
                out[c["id"]] = {"section": c["section"], "reason": c.get("reason", "")}
    usage = resp.usage
    return out, {
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def classify(rows: list, model: str, use_cache: bool = True, quiet: bool = False) -> tuple:
    """Predict a section for every row, reusing cached verdicts where the prompt is unchanged."""
    preds, counts = {}, {"input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                         "cache_hits": 0, "requests": 0, "errors": 0}

    pending = []
    for r in rows:
        hit = _cache_get(_cache_key(model, r)) if use_cache else None
        if hit:
            preds[r["id"]] = hit
            counts["cache_hits"] += 1
        else:
            pending.append(r)

    if not pending:
        if not quiet:
            print(f"  all {len(rows)} verdicts served from cache (no API calls, $0)")
        return preds, counts

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        got, usage = _classify_batch(client, model, batch)
        counts["requests"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read"):
            counts[k] += usage[k]
        for r in batch:
            verdict = got.get(r["id"])
            if verdict is None:
                counts["errors"] += 1  # model skipped it; scored as wrong, tracked separately
                continue
            preds[r["id"]] = verdict
            if use_cache:
                _cache_put(_cache_key(model, r), verdict)
        if not quiet:
            print(f"  batch {counts['requests']}: {len(batch)} items "
                  f"({counts['cache_read']} cached tokens read)")
    return preds, counts


def score(rows: list, preds: dict) -> dict:
    """Label comparison: accuracy, per-class P/R/F1, per-tag accuracy, confusion, failures."""
    confusion = {g: {} for g in LABELS}
    correct = 0
    failures = []

    for r in rows:
        gold = r["gold_section"]
        pred = (preds.get(r["id"]) or {}).get("section")
        confusion.setdefault(gold, {})
        confusion[gold][pred] = confusion[gold].get(pred, 0) + 1
        if pred == gold:
            correct += 1
        else:
            failures.append({"id": r["id"], "gold": gold, "pred": pred, "tags": r["tags"],
                             "headline": r["headline"][:90],
                             "reason": (preds.get(r["id"]) or {}).get("reason", "")})

    by_section, f1s = {}, []
    for label in LABELS:
        support = sum(1 for r in rows if r["gold_section"] == label)
        tp = sum(1 for r in rows
                 if r["gold_section"] == label and (preds.get(r["id"]) or {}).get("section") == label)
        predicted = sum(1 for r in rows if (preds.get(r["id"]) or {}).get("section") == label)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        by_section[label] = {"precision": round(precision, 4), "recall": round(recall, 4),
                            "f1": round(f1, 4), "support": support}
        if support:  # a class with no gold examples can't be averaged over honestly
            f1s.append(f1)

    by_tag = {}
    for tag in sorted({t for r in rows for t in r["tags"]}):
        tagged = [r for r in rows if tag in r["tags"]]
        hits = sum(1 for r in tagged
                   if (preds.get(r["id"]) or {}).get("section") == r["gold_section"])
        by_tag[tag] = {"accuracy": round(hits / len(tagged), 4), "support": len(tagged)}

    metrics = {
        "accuracy": round(correct / len(rows), 4) if rows else 0.0,
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "drop_recall": by_section[DROP]["recall"],
    }
    # Per-section recall is promoted into gated metrics; per-tag stays report-only, because
    # a tag bucket of ~5 flips 20 points on one item and would flap the gate.
    for s in SECTIONS:
        if by_section[s]["support"]:
            metrics[f"recall.{s}"] = by_section[s]["recall"]

    return {"metrics": metrics, "breakdowns": {"by_section": by_section, "by_tag": by_tag},
            "confusion": confusion, "failures": failures}


def run(args) -> dict:
    """Entry point used by evals/run_evals.py."""
    from evals.label import load_dataset

    rows = [r for r in load_dataset(args.dataset) if r.get("reviewed")] \
        if args.require_human else load_dataset(args.dataset)
    if not rows:
        raise SystemExit(f"no reviewed rows in {args.dataset} — "
                         f"label some first with `python -m evals.label review`")
    if args.limit:
        rows = rows[:args.limit]

    model = args.model or config.MODEL
    use_cache = not args.no_cache
    uncached = sum(1 for r in rows if not (use_cache and _cache_get(_cache_key(model, r))))

    if uncached:
        config.require_anthropic_key()
        est = estimate_cost(uncached, model)
        print(f"  {len(rows)} rows ({uncached} need the API) on {model} — est. ${est:.3f}")
        if not args.yes and os.environ.get("EVAL_CONFIRM") != "1" and sys.stdin.isatty():
            if input("  proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                raise SystemExit("aborted")

    started = time.time()
    preds, counts = classify(rows, model, use_cache=use_cache, quiet=args.quiet)
    result = score(rows, preds)

    price_in, price_out = _PRICING.get(model, (3.0, 15.0))
    counts["cost_usd"] = round(
        (counts["input_tokens"] * price_in + counts["output_tokens"] * price_out) / 1_000_000, 4)
    counts["items"] = len(rows)
    counts["duration_s"] = round(time.time() - started, 2)

    result["counts"] = counts
    result["dataset"] = {"path": args.dataset, "n": len(rows),
                         "reviewed_only": bool(args.require_human)}
    return result
