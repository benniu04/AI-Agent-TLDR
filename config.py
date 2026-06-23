"""Central configuration: env-var loading and run bounds.

Everything the agent needs to be told (keys, model) and everything that bounds
its behaviour (iteration / token / wall-clock caps) is defined here so there is a
single place to tune cost and safety.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # load .env if present; in CI the vars come from real env / secrets


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


# --- Credentials & model ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

# How many headlines per section make it into the delivered briefing.
MAX_HEADLINES_PER_SECTION = _int("MAX_HEADLINES_PER_SECTION", 5)
# Money Movement gets a higher cap — it's the user's priority beat and we want it fuller when
# supply allows. It's a ceiling, not a target (the brief still says don't pad with junk).
MAX_HEADLINES_MONEY_MOVEMENT = _int("MAX_HEADLINES_MONEY_MOVEMENT", 8)
# IANA timezone for the digest's date label (the model has no reliable clock, so we stamp
# the date ourselves). Default Eastern to match the 9am-ET schedule.
TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")

# Hard recency backstop: drop any story whose source publish date is older than this many
# days (only applies to items with a known timestamp; web_search items fail open). 3 days
# catches clearly-stale events (e.g. a 4-day-old filing) without nixing weekend stories.
MAX_STORY_AGE_DAYS = _int("MAX_STORY_AGE_DAYS", 3)

# Cross-run memory: a rolling JSON record of delivered items so the agent doesn't repeat
# stories day to day (committed back to the repo by CI since Actions is ephemeral).
MEMORY_PATH = os.environ.get("MEMORY_PATH", "memory/seen.json")
# How many days of delivered items to remember (the hard repeat filter + the goal's
# "already covered" list both look back this far). Slightly longer than MAX_STORY_AGE_DAYS so
# a story can't drop out of memory while it's still recent enough to resurface.
MEMORY_KEEP_DAYS = _int("MEMORY_KEEP_DAYS", 7)

# Telegram delivery channel.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Finance news APIs (free tiers). Optional — get_finance_news uses whichever keys are set
# and falls back to web_search if neither is configured.
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")

# --- Per-call sizing ---
# Headroom so the final submit_tldr tool call can't be truncated. Output is only billed
# for what's generated, so a larger ceiling doesn't itself cost more.
MAX_TOKENS_PER_CALL = _int("MAX_TOKENS_PER_CALL", 16000)

# --- Run bounds (the guardrails that keep a runaway agent from burning money) ---
MAX_ITERATIONS = _int("MAX_ITERATIONS", 15)
# Cumulative input+output tokens across all calls in one run before we stop.
# ~140k for a normal 8-search run; headroom covers a max_tokens recovery turn.
MAX_TOKENS_BUDGET = _int("MAX_TOKENS_BUDGET", 300_000)
# Normal runs take ~4 min; 8 min gives headroom so a slow run isn't killed mid-gather before
# it can submit. The agent also self-nudges to wrap up at 85% of this. Keep below the
# workflow's job timeout-minutes (12) so our graceful stop fires before GitHub's hard kill.
WALL_CLOCK_SECONDS = _int("WALL_CLOCK_SECONDS", 480)


def require_anthropic_key() -> str:
    """Validate the Anthropic key right before we actually call the API."""
    return _require("ANTHROPIC_API_KEY")


def require_telegram() -> tuple[str, str]:
    """Validate Telegram creds right before we actually try to deliver."""
    return _require("TELEGRAM_BOT_TOKEN"), _require("TELEGRAM_CHAT_ID")
