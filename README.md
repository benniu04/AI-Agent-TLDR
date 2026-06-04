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

The agent's final output is **structured JSON** (headlines + source URLs, no prose). The
harness formats that into a glanceable briefing for the chosen channel. Set `DELIVERY` to
`sms` (Twilio, default) or `telegram`.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in the values
```

`.env` keys: `ANTHROPIC_API_KEY`, `MODEL`, `DELIVERY` (`sms`|`telegram`),
`MAX_HEADLINES_PER_SECTION`, plus the creds for your chosen channel.

### SMS via Twilio (DELIVERY=sms)
Headlines + tappable links land in the native Messages app. Output is ASCII-sanitized so it
stays in cheap 160-char segments (a single emoji would otherwise double the cost).
1. Create a [Twilio](https://www.twilio.com/try-twilio) account and buy a phone number.
2. For texting **other people's** US numbers, register **A2P 10DLC** (one-time ~$4 + ~$2/mo);
   on a trial you can text only numbers you've verified.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, and
   `SMS_RECIPIENTS` (comma-separated E.164 numbers, e.g. `+15551234567`).

Rough cost: a headlines-only digest is ~1–2 segments-per-section, ≈ $0.10/day per recipient.

### Telegram (DELIVERY=telegram) — free fallback
Create a bot with [@BotFather](https://t.me/BotFather) for the token; message it, then read
your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`. For multiple readers,
point `TELEGRAM_CHAT_ID` at a channel the bot admins.

## Run

```bash
.venv/bin/python run.py --dry-run   # research + print the formatted TLDR, no send
.venv/bin/python run.py             # full run + deliver via $DELIVERY
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
manual `workflow_dispatch`. Add `ANTHROPIC_API_KEY` and your channel's secrets
(`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `SMS_RECIPIENTS` for SMS;
`TELEGRAM_*` for Telegram) as repo **secrets**, and optionally `MODEL` / `DELIVERY` as repo
**variables**. Cron is UTC — pick the UTC time matching your local send time; expect ±1h
drift across daylight-saving changes.

## Deferred follow-ons

Memory (skip repeats / flag follow-ups), Agent SDK migration, and a richer finance tool
(Finnhub / Alpha Vantage MCP) — see the plan for details.
