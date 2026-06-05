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

# Delivery channel: "sms" (Twilio) or "telegram". Validated at delivery time only.
DELIVERY = os.environ.get("DELIVERY", "sms").lower()
# How many headlines per section make it into the delivered briefing.
MAX_HEADLINES_PER_SECTION = _int("MAX_HEADLINES_PER_SECTION", 5)

# Telegram (free fallback channel).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Twilio SMS. SMS_RECIPIENTS is a comma-separated list of E.164 numbers (+15551234567).
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
SMS_RECIPIENTS = [n.strip() for n in os.environ.get("SMS_RECIPIENTS", "").split(",") if n.strip()]

# --- Per-call sizing ---
# Headroom so the final submit_tldr tool call can't be truncated. Output is only billed
# for what's generated, so a larger ceiling doesn't itself cost more.
MAX_TOKENS_PER_CALL = _int("MAX_TOKENS_PER_CALL", 16000)

# --- Run bounds (the guardrails that keep a runaway agent from burning money) ---
MAX_ITERATIONS = _int("MAX_ITERATIONS", 15)
# Cumulative input+output tokens across all calls in one run before we stop.
MAX_TOKENS_BUDGET = _int("MAX_TOKENS_BUDGET", 250_000)
WALL_CLOCK_SECONDS = _int("WALL_CLOCK_SECONDS", 180)


def require_anthropic_key() -> str:
    """Validate the Anthropic key right before we actually call the API."""
    return _require("ANTHROPIC_API_KEY")


def require_telegram() -> tuple[str, str]:
    """Validate Telegram creds right before we actually try to deliver."""
    return _require("TELEGRAM_BOT_TOKEN"), _require("TELEGRAM_CHAT_ID")


def require_twilio() -> tuple[str, str, str, list[str]]:
    """Validate Twilio creds + at least one recipient right before delivery."""
    sid = _require("TWILIO_ACCOUNT_SID")
    token = _require("TWILIO_AUTH_TOKEN")
    from_number = _require("TWILIO_FROM_NUMBER")
    if not SMS_RECIPIENTS:
        raise RuntimeError("Missing SMS_RECIPIENTS (comma-separated E.164 numbers).")
    return sid, token, from_number, SMS_RECIPIENTS
