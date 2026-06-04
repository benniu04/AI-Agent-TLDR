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

# Telegram is only needed at delivery time, so don't hard-require it on import.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Per-call sizing ---
MAX_TOKENS_PER_CALL = _int("MAX_TOKENS_PER_CALL", 4096)

# --- Run bounds (the guardrails that keep a runaway agent from burning money) ---
MAX_ITERATIONS = _int("MAX_ITERATIONS", 15)
# Cumulative input+output tokens across all calls in one run before we stop.
MAX_TOKENS_BUDGET = _int("MAX_TOKENS_BUDGET", 200_000)
WALL_CLOCK_SECONDS = _int("WALL_CLOCK_SECONDS", 180)


def require_anthropic_key() -> str:
    """Validate the Anthropic key right before we actually call the API."""
    return _require("ANTHROPIC_API_KEY")


def require_telegram() -> tuple[str, str]:
    """Validate Telegram creds right before we actually try to deliver."""
    return _require("TELEGRAM_BOT_TOKEN"), _require("TELEGRAM_CHAT_ID")
