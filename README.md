# Daily TLDR Agent

An autonomous agent that assembles a glanceable daily finance/markets/tech news briefing and
delivers it to your phone via **Telegram** every weekday morning. Built as a **true agent**, 
the model decides which tools to call and when it's done, with a **hand-written tool-use loop**
(Anthropic Messages API), not an off-the-shelf framework. The goal was to understand agent
mechanics end-to-end and wrap them in the deterministic guardrails a real product needs: link
integrity, recency, repeat-suppression, cost control, and observability.

The digest is **headlines only** and no summaries. You skim five sections in a few seconds and
tap a link only if a story is worth your time.

```
📰 Daily TLDR — Tue, Jun 9

💰 Finance
• GSK acquires Nuvalent for $10.6B to fast-track lung cancer pipeline
• OpenAI confidentially files for IPO at $850B+ valuation
...

💸 Money Movement
• Zodia Custody secures Luxembourg license to expand EU stablecoin services
• Ecommpay introduces express checkout for Google Pay and Apple Pay
...

🌊 Liquidity
• Futures traders price out all 2026 Fed cuts ahead of June FOMC
...

🤖 AI   💻 Tech   (…)
```

## Why it's interesting (engineering)

- **Hand-written agent loop** ([`agent.py`](agent.py)) — drives the Anthropic Messages API
  directly, handling every stop reason: `tool_use` (run the client tool, feed results back),
  `pause_turn` (a server tool is mid-flight — resend), `end_turn`, and `max_tokens` (recover
  and force a clean submit). Bounded by iteration, cumulative-token, and wall-clock caps, with
  a soft-deadline nudge and 429 retry/backoff so a slow run still ships something.
- **Server tools vs. client tools** — `web_search` / `web_fetch` execute inside one API turn
  (Anthropic-side) and arrive as `server_tool_use` blocks; the loop only *dispatches* custom
  client tools (the news-source APIs). This distinction is core to how the loop is written.
- **Structured output, not text parsing** — the agent finishes by calling a terminal
  `submit_tldr` tool whose schema *is* the briefing, so the result is validated structured
  data instead of brittle JSON-in-prose parsing.
- **Deterministic link-integrity pipeline** ([`formatting.py`](formatting.py)) — the model's
  submission passes through layered `$0` filters before anything ships (details below). The
  model decides what's *worth* including; the code enforces what's *allowed* to ship.
- **Cross-run memory** ([`memory.py`](memory.py)) — a rolling 7-day record of delivered items
  (committed back to the repo by CI) suppresses day-over-day repeats, both deterministically
  (drop any URL already sent) and softly (recent headlines fed into the goal).
- **Prompt caching** — the static system+tools prefix and the growing conversation are cached
  with rolling breakpoints, so re-sent context bills at ~10% instead of full price (~30% lower
  cost per run, byte-identical output).
- **Cost-aware sourcing** — a run's cost is almost entirely `web_search` tokens, so dedicated
  free news APIs/feeds are primary and `web_search` is a capped fallback.
- **Observability + alerting** — every step (stop reason, tool calls, result URLs, token and
  cache usage) is logged to a JSONL trajectory in `logs/`; a failed run sends a Telegram alert
  so a broken pipeline is never silent.

## Architecture

```
                     ┌─────────────────────────────────────────────┐
   build_goal()  ──► │  agent loop (agent.py)                       │
   + SYSTEM brief    │   model picks tools ──► dispatch ──► results │
   + recent memory   │   ▲                                    │     │
                     │   └──────────── feed back ─────────────┘     │
                     │   stop reasons: tool_use / pause_turn /      │
                     │   end_turn / max_tokens                      │
                     └───────────────────┬─────────────────────────┘
                                         │ submit_tldr (structured)
                                         ▼
   source tools (tools.py)        link-integrity filters (formatting.py)     delivery
   ─ get_finance_news  (Finnhub    1. banned / index-page drop               ─ Telegram (HTML)
      + Alpha Vantage sentiment)   2. headline⇄URL keyword match
   ─ get_payments_news (RSS)       3. provenance (URL actually seen?)
   ─ get_liquidity_news(AV+WSJ)    4. recency (timestamp < cutoff)           run.py orchestrates;
   ─ get_ai_news / get_tech_news   5. cross-run repeat (memory)              delivery happens only
   ─ get_hacker_news               6. exact-URL + cross-section topic dedup   AFTER the agent
   ─ web_search / web_fetch         7. per-section cap                        finishes
     (fallback)
```

### Sections & sourcing

Five beats, each with a dedicated primary source and `web_search` as fallback:

| Section | Focus | Primary source |
|---|---|---|
| **Finance** | Earnings, IPOs, M&A, economic data | Finnhub + Alpha Vantage (sentiment) |
| **Money Movement** | Consumer payments / P2P (Zelle, FedNow/RTP, card networks, stablecoin settlement, payments fraud) | Finextra, PYMNTS, Payments Dive, Banking Dive RSS |
| **Liquidity** | Funding / monetary (Fed rates, repo, reserves, deposits, credit, Treasuries) | Alpha Vantage `economy_monetary` + WSJ Markets + Fed press RSS |
| **AI** | Model/capability releases, funding, policy | AI RSS feeds + Hacker News |
| **Tech** | Shipped products, security, major company moves | Tech RSS feeds + Hacker News |

Money Movement and Liquidity are framed through the lens of *what a banking professional tracks*
(the project is tuned for a Bank of America payments perspective), with explicit
section-ownership rules so the three finance-adjacent sections don't claim each other's stories.

### The link-integrity pipeline

Models occasionally fabricate a URL, paste a wrong link on a headline, or resurface an old
story. The pipeline in `_iter_sections` makes that impossible to ship, deterministically and
for free:

1. **Banned / index-page drop** — live blogs, daily recaps, aggregators, bare section pages.
2. **Headline⇄URL keyword match** — drops a headline whose URL is about a *different* story
   (with a `.gov` exemption for opaque primary-source slugs like BLS).
3. **Provenance** — drops any URL that never actually appeared in this run's search/feed
   results (i.e. fabricated from memory). *Fails open* if the seen-set is empty.
4. **Recency** — drops feed/API items whose publish timestamp is older than `MAX_STORY_AGE_DAYS`.
5. **Cross-run repeat** — drops any URL already delivered in the last `MEMORY_KEEP_DAYS`.
6. **Dedup** — exact-URL dedup plus cross-section *topic* dedup (two headlines sharing ≥2
   distinctive words collapse to one).
7. **Per-section cap** — trims to `MAX_HEADLINES_PER_SECTION`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in keys
```

Required: `ANTHROPIC_API_KEY`, plus Telegram delivery (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
— create a bot via [@BotFather](https://t.me/BotFather); point the chat id at a channel the bot
admins to let others subscribe). Finance APIs (`FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY`) are
optional but recommended — without them, finance/liquidity fall back to `web_search`.

## Run

```bash
python run.py            # build the briefing and deliver to Telegram
python run.py --dry-run  # build and print, but do NOT send or record memory
```

Delivery is **not** a tool — `run.py` sends only after the agent has fully finished, keeping the
one irreversible action out of the model's hands.

## Test

The deterministic filter pipeline + helpers are covered by a unit suite (no network or API keys
needed):

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Evals

The unit suite covers the deterministic filter chain — the part that runs *after* the model has
decided. The evals cover the part it can't: the editorial judgment in `prompts.py`, which is where
nearly all the iteration happens.

| Suite | Grades | Cost | Runs |
|---|---|---|---|
| `digest` | a recorded run against the pipeline's own rules | free | every push/PR |
| `routing` | `prompts.py`'s section policy vs ~160 labeled headlines | ~$0.04 | on demand |

```bash
python -m evals.run_evals digest              # score the committed reference bundles
python -m evals.run_evals digest --gate       # exit 2 if any metric fell below the baseline
python -m evals.run_evals digest --update-baseline

python -m evals.run_evals routing                            # score the shipping model
python -m evals.run_evals routing --model claude-sonnet-4-6  # sweep another one
```

### The routing eval

Sends `prompts.SYSTEM` **verbatim** as the system prompt and asks the model where each labeled
headline belongs. Testing the real artifact rather than a paraphrase is the whole design: edit a
tier in `prompts.py` and the number moves on the next run, with nothing to keep in sync.

Determinism comes from a prediction cache keyed on the prompt hash — not `temperature=0`, which
current models reject outright. An unchanged re-run is free and byte-identical; editing
`prompts.py` invalidates every verdict, which is exactly the invalidation you want.

Building the labels:

```bash
python -m evals.capture                  # freeze today's candidate pool (free, ~no keys)
python -m evals.label bootstrap          # seed rows with a provisional guess
python -m evals.label review             # confirm each one — only these are scored
python -m evals.label stats              # class + tag coverage
```

The feed a story arrived on is a weak prior and is wrong about 40% of the time, which is why
`bootstrap` guesses are never scored: the suite reads `reviewed: true` rows only.

### Auditing the labels

A routing run doubles as an audit of the dataset. Every disagreement is either a model error or a
label error, and the model's stated reason usually settles which within seconds:

```bash
python -m evals.label review --disputed   # queue only the contested rows
```

Press `k` to uphold your label or a section key to correct it; either way the row records what
happened and which model challenged it. Expect the first run to find more label bugs than model
bugs — that's the normal outcome of a first eval, not a setback.

**Why it grades the raw submission, not the delivered digest.** `formatting._iter_sections` is a
*filter*: a banned URL, a duplicate, a stale story, an over-cap item — each is silently dropped
before delivery. Grading what shipped would score ~100% on almost every rule by construction. So
the graders score the untouched `submit_tldr` payload as a drop rate (`1 - bad/total`), which
measures how much correcting the model needed. On the committed reference bundle that's
`digest_integrity 0.958` — a real run where the model submitted three banned URLs and three
borrowed links that the filter caught and no one ever saw.

Every metric is a 0–1 rate where higher is better, so the gate is one-directional and generic:
any metric that falls more than 2pp below `evals/baselines/<suite>.json` fails the build. Counts
that aren't on that scale (items, thin sections) live in `counts` and are never gated — in
particular section fill, because the brief explicitly prefers a short section to a padded one.

Production runs record their own bundle with `python run.py --save-run`; in CI it rides out as an
artifact rather than a commit, so one weak news day can't shift the mean and red-build unrelated
PRs. Extending the reference set is a deliberate, reviewed act: commit a bundle and re-record the
baseline in the same PR.

## Deploy

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs the agent via
`workflow_dispatch` (keys from repo **secrets**, model/timezone from repo **variables**) and:

- **Scheduling is external** — an off-platform scheduler (e.g. [cron-job.org](https://cron-job.org))
  calls the `workflow_dispatch` API at **9:00 AM ET on weekdays**. GitHub's native `schedule:`
  cron was removed because it fired 1–2 hours late (best-effort scheduler); an external trigger
  is on time and timezone-aware (no DST drift).
- **Cross-run memory persists** — after a successful run a step commits the updated
  `memory/seen.json` back to the repo (with rebase + retry, non-fatal), since Actions runners
  are ephemeral.
- **Failure alerting** — a final `if: failure()` step sends a Telegram message with a logs link,
  so a broken run is never silent.
- **Free smoke mode** — `gh workflow run "Daily TLDR" -f smoke=true` skips the paid agent step
  and verifies the plumbing (actions, deps, push auth) for $0; also a free connectivity test.

## Configuration

All knobs live in [`config.py`](config.py) (override via env): `MODEL`,
`MAX_HEADLINES_PER_SECTION`, `MAX_STORY_AGE_DAYS`, `MEMORY_KEEP_DAYS`, `TIMEZONE`, and the run
guardrails (`MAX_ITERATIONS`, `MAX_TOKENS_BUDGET`, `WALL_CLOCK_SECONDS`, `MAX_TOKENS_PER_CALL`).

## Tech stack

Python · Anthropic Messages API (tool use, server tools, prompt caching) · feedparser ·
requests · GitHub Actions · Telegram Bot API.

## Roadmap

- **Per-section dedup scoping** if the global topic-dedup ever starves a section.
- **Richer P2P/fraud sourcing** for the Money Movement beat on thin days.
- **Agent SDK migration** once the hand-written loop has served its learning purpose.
