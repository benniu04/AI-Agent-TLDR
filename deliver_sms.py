"""Twilio SMS delivery — a HARNESS action, not a tool.

Like Telegram delivery, this runs only after the agent has finished. We send the same
text to every recipient in SMS_RECIPIENTS. Twilio caps a single message body at 1600
chars, so we chunk on line boundaries and send the digest as one or more texts.
"""

import config

# Twilio's hard per-message body limit. Stay under it to avoid API errors.
TWILIO_BODY_LIMIT = 1500


def _chunks(text: str, size: int = TWILIO_BODY_LIMIT):
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def send_sms(text: str) -> None:
    """Send `text` (already ASCII-sanitized by formatting.format_sms) to all recipients."""
    from twilio.rest import Client  # imported lazily so the dep isn't needed for Telegram

    sid, token, from_number, recipients = config.require_twilio()
    client = Client(sid, token)

    parts = list(_chunks(text))
    for to in recipients:
        for i, body in enumerate(parts):
            # Prefix multi-part sends so the reader knows there's more (1/2, 2/2).
            prefix = f"({i + 1}/{len(parts)}) " if len(parts) > 1 else ""
            client.messages.create(body=prefix + body, from_=from_number, to=to)
