# Daily TLDR Agent

An autonomous agent that assembles a glanceable daily finance/markets/tech news briefing and
pushes it to your phone every morning. Built as a **true agent** — the model decides which
tools to call and when it's done — with a **hand-written tool-use loop** (Anthropic Messages
API), not an off-the-shelf framework. The goal was to understand agent mechanics end-to-end
and wrap them in the deterministic guardrails a real product needs: link integrity, recency,
cost control, and observability.

The digest is **headlines only** — no summaries. You skim five sections in a few seconds and
tap a link only if a story is worth your time.

```
📰 Daily TLDR — Fri, Jun 5

💰 Finance
• SpaceX IPO roadshow launches at $135/share, targeting record raise on June 12
• Lululemon cuts full-year outlook, issues weak Q2 guidance
...

💸 Money Movement
• JPMorgan, Citi, BofA, Wells Fargo plan shared tokenized deposit network via The Clearing House
• Airwallex acquires financial-data automation platform Leapfin
...

🌊 Liquidity
• Treasury yields and dollar jump after May payrolls smash expectations
• Blackstone investors seek to pull $4.4B from giant private-credit fund
...

🤖 AI   💻 Tech   (…)
```

## Why it's interesting (engineering)

- **Hand-written agent loop** ([`agent.py`](agent.py)) — drives the Anthropic Messages API
  directly, handling every stop reason: `tool_use` (run the client tool, feed results back),
  `pause_turn` (a server tool is mid-flight — resend), `end_turn`, and `max_tokens` (recover
  and force a clean submit). Every run is bounded by iteration, cumulative-token, and
  wall-clock caps, with 429 rate-limit retry/backoff.
- **Server tools vs. client tools** — `web_search` / `web_fetch` execute inside one API turn
  (Anthropic-side) and arrive as `server_tool_use` blocks; the loop only *dispatches* custom
  client tools (the news-source APIs). This distinction is core to how the loop is written.
- **Structured output, not text parsing** — the agent finishes by calling a terminal
  `submit_tldr` tool whose schema *is* the briefing, so the result is validated structured
  data instead of brittle JSON-in-prose parsing.
- **Deterministic link-integrity pipeline** ([`formatting.py`](formatting.py)) — the model's
  submission passes through layered `$0` filters before anything ships (details below). The
  model decides what's *worth* including; the code enforces what's *allowed* to ship.
- **Cost-aware sourcing** — a run's cost is almost entirely `web_search` tokens, so dedicated
  free news APIs/feeds are primary and `web_search` is a capped fallback — roughly halving
  per-run cost.
- **Observability** — every step (stop reason, tool calls + inputs, result URLs, cumulative
  usage) is written to a JSONL trajectory in `logs/` for debugging non-deterministic runs.

## Architecture

```
                     ┌─────────────────────────────────────────────┐
   build_goal()  ──► │  agent loop (agent.py)                       │
   + SYSTEM brief    │   model picks tools ──► dispatch ──► results │
                     │   ▲                                    │     │
                     │   └──────────── feed back ─────────────┘     │
                     │   stop reasons: tool_use / pause_turn /      │
                     │   end_turn / max_tokens                      │
                     └───────────────────┬─────────────────────────┘
                                         │ submit_tldr (structured)
                                         ▼
   source tools (tools.py)        link-integrity filters (formatting.py)      delivery
   ─ get_finance_news  (Finnhub    1. banned / index-page drop                ─ Telegram
      + Alpha Vantage sentiment)   2. headline⇄URL keyword match              ─ SMS (Twilio)
   ─ get_payments_news (RSS)       3. provenance (URL actually seen?)
   ─ get_liquidity_news(AV+WSJ)    4. recency (timestamp < cutoff)            run.py orchestrates;
   ─ get_ai_news / get_tech_news   5. exact-URL + cross-section topic dedup   delivery happens only
   ─ get_hacker_news               6. per-section cap                         AFTER the agent finishes
   ─ web_search / web_fetch (fallback)
```

### Sections & sourcing

Five beats, each with a dedicated primary source and `web_search` as fallback:

| Section | Focus | Primary source |
|---|---|---|
| **Finance** | Earnings, IPOs, M&A, economic data | Finnhub + Alpha Vantage (sentiment) |
| **Money Movement** | Payments / transaction banking (FedNow, card networks, Zelle, stablecoin settlement, cross-border) | Finextra, PYMNTS, Payments Dive RSS |
| **Liquidity** | Funding / monetary (Fed rates, repo, reserves, deposits, credit, Treasuries) | Alpha Vantage `economy_monetary` + WSJ Markets RSS |
| **AI** | Model/capability releases, funding, policy | AI RSS feeds + Hacker News |
| **Tech** | Shipped products, security, major company moves | Tech RSS feeds + Hacker News |

Money Movement and Liquidity are framed through the lens of *what a professional at a major US
bank tracks*, with explicit section-ownership rules so the three finance-adjacent sections
don't claim each other's stories.

### The link-integrity pipeline

Models occasionally fabricate a URL, paste a wrong link on a headline, or resurface a days-old
story. The pipeline in `_iter_sections` makes that impossible to ship, deterministically and
for free:

1. **Banned / index-page drop** — live blogs, daily recaps, aggregators, bare section pages.
2. **Headline⇄URL keyword match** — drops a headline whose URL is about a *different* story
   (with a `.gov` exemption for opaque primary-source slugs like BLS).
3. **Provenance** — drops any URL that never actually appeared in this run's search/feed
   results (i.e. fabricated from memory). *Fails open* if the seen-set is empty.
4. **Recency** — drops feed/API items whose publish timestamp is older than `MAX_STORY_AGE_DAYS`.
5. **Dedup** — exact-URL dedup plus cross-section *topic* dedup (two headlines sharing ≥2
   distinctive words collapse to one).
6. **Per-section cap** — trims to `MAX_HEADLINES_PER_SECTION`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in keys
```

Required: `ANTHROPIC_API_KEY`. For delivery, either Telegram (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID` — create a bot via [@BotFather](https://t.me/BotFather)) or Twilio SMS
(`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `SMS_RECIPIENTS`). Finance
APIs (`FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY`) are optional but recommended — without them
finance/liquidity fall back to `web_search`.

## Run

```bash
python run.py            # build briefing and deliver via $DELIVERY (telegram | sms)
python run.py --dry-run  # build and print, but do NOT send
```

Delivery is **not** a tool — `run.py` sends only after the agent has fully finished, keeping
the one irreversible action out of the model's hands. SMS output is ASCII-sanitized so it
stays in cheap 160-char GSM-7 segments.

## Test

The deterministic filter pipeline is covered by a unit suite (no network or API keys needed):

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Deploy

[`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs the agent on a cron schedule
(GitHub Actions, UTC) and via manual `workflow_dispatch`, uploading the trajectory log as an
artifact. Keys come from repo **secrets**; model/channel/timezone from repo **variables**. The
cron deliberately avoids the top of the hour, where GitHub's best-effort scheduler is most
likely to delay or drop a run; cron is UTC, so expect ±1h drift across daylight-saving changes.

## Configuration

All knobs live in [`config.py`](config.py) (override via env): `MODEL`, `DELIVERY`,
`MAX_HEADLINES_PER_SECTION`, `MAX_STORY_AGE_DAYS`, `TIMEZONE`, and the run guardrails
(`MAX_ITERATIONS`, `MAX_TOKENS_BUDGET`, `WALL_CLOCK_SECONDS`, `MAX_TOKENS_PER_CALL`).

## Tech stack

Python · Anthropic Messages API (tool use, server tools) · feedparser · requests · GitHub
Actions · Telegram Bot API / Twilio.

## Roadmap

- **Cross-run memory** — persist delivered headlines/URLs so the agent skips day-over-day
  repeats and flags genuine follow-ups (in progress).
- **Per-section dedup scoping** if the global topic-dedup ever starves a section.
- **Agent SDK migration** once the hand-written loop has served its learning purpose.
