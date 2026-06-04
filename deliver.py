"""Telegram delivery — a HARNESS action, deliberately NOT a tool.

The agent returns the finished TLDR as text; only after it's done does run.py call
send_telegram(). Keeping the one irreversible action (pushing to the phone) out of the
model's tool set means the agent can never send a half-finished or runaway briefing.
"""

import requests

import config

TELEGRAM_LIMIT = 4096  # max chars per Telegram message
HTTP_TIMEOUT = 15


def _chunks(text: str, size: int = TELEGRAM_LIMIT):
    """Split on paragraph/line boundaries where possible, hard-split as a fallback."""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def send_telegram(text: str) -> None:
    """Send `text` to the configured chat, chunked to Telegram's size limit."""
    token, chat_id = config.require_telegram()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _chunks(text):
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram send failed ({resp.status_code}): {resp.text}")
