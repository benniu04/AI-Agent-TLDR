# Daily TLDR Agent

An autonomous agent that assembles a glanceable daily finance/markets/tech news briefing and
delivers it to your phone via **Telegram** every weekday morning. Built as a **true agent** —
the model decides which tools to call and when it's done — with a **hand-written tool-use loop**
(Anthropic Messages API), not an off-the-shelf framework. The goal was to understand agent
mechanics end-to-end and wrap them in the deterministic guardrails a real product needs: link
integrity, recency, repeat-suppression, cost control, and observability.

The digest is **headlines only** — no summaries. You skim five sections in a few seconds and
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

## Deploy

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs the agent via
`workflow_dispatch` (keys from repo **secrets**, model/timezone from repo **variables**) and:

- **Scheduling is external** — an off-platform scheduler (e.g. [cron-job.org](https://cron-job.org))
  calls the `workflow_dispatch` API at **8:20 AM ET on weekdays**. GitHub's native `schedule:`
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
