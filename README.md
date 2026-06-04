# Daily TLDR Agent

A small **agent** (not a fixed script) that assembles a daily finance / AI / tech briefing
and pushes it to your phone via Telegram. The model is given a goal and tools, then runs in
a hand-written loop — it decides which tools to call and when the briefing is done.

## How it works

`run.py` → `run_agent()` (the loop in `agent.py`) → `send_telegram()` (`deliver.py`).

The loop hands the model a goal + tools and repeats: call the API, run any custom tool the
model requested, feed the result back — until the model returns `end_turn` with the finished
TLDR. Delivery is **not** a tool: the harness sends the result only after the agent finishes,
keeping the one irreversible action out of the model's hands.

Tools:
- `web_search`, `web_fetch` — Anthropic **server** tools (run on Anthropic's side inside one turn).
- `get_hacker_news` — a **custom client** tool we execute (HN API, no key); seeds tech coverage.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in the values
```

`.env` keys: `ANTHROPIC_API_KEY`, `MODEL` (default `claude-sonnet-4-6`),
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

**Telegram:** create a bot with [@BotFather](https://t.me/BotFather) to get the token; send it
a message, then read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates` (`result[].message.chat.id`).

## Run

```bash
.venv/bin/python run.py --dry-run   # research + print TLDR, no Telegram send
.venv/bin/python run.py             # full run + deliver to Telegram
.venv/bin/python agent.py           # Phase-1 smoke test (one tool, toy goal)
```

Every run writes a full trajectory to `logs/<timestamp>.jsonl` (every step, tool call, and
cumulative token count) so you can see exactly what the agent did.

## Bounds (in `config.py`, override via env)

`MAX_ITERATIONS=15`, `MAX_TOKENS_BUDGET=200000`, `WALL_CLOCK_SECONDS=180`. The loop also
retries on HTTP 429 with backoff — a single run pulls ~140k tokens of search results, so
low API tiers can hit a per-minute input-token cap on rapid back-to-back runs.

## Schedule

`.github/workflows/daily.yml` runs `run.py` on a UTC cron (default `0 12 * * *`) and via
manual `workflow_dispatch`. Add `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
as repo **secrets** (and optionally `MODEL` as a repo **variable**). Cron is UTC — pick the
UTC time matching your local send time; expect ±1h drift across daylight-saving changes.

## Deferred follow-ons

Memory (skip repeats / flag follow-ups), Agent SDK migration, and a richer finance tool
(Finnhub / Alpha Vantage MCP) — see the plan for details.
