"""Slack Socket Mode entry point: python -m stiracore.integrations.slack_bot."""

from __future__ import annotations

import logging
import os

from .runtime import ChatSessions, chunks, status_text

logger = logging.getLogger(__name__)


def register_handlers(app, sessions: ChatSessions) -> None:
    """Register handlers separately so tests need neither Slack nor AWS."""

    @app.event("message")
    def on_message(event, say, body):
        # Ignore edits, bot/webhook messages and non-text events (avoid reply loops).
        if event.get("bot_id") or event.get("subtype") or not event.get("user"):
            return
        text = event.get("text", "").strip()
        if not text:
            return
        channel = event["channel"]
        key = f"{body.get('team_id', '')}:{channel}"
        result = sessions.get(key).runner(text)
        thread = event.get("thread_ts") or event["ts"]
        for part in chunks(result.text, 3000):
            # Plain text prevents model output from generating @channel mentions.
            say(
                channel=channel,
                thread_ts=thread,
                text="Agent response",
                blocks=[{"type": "section", "text": {"type": "plain_text", "text": part}}],
            )
        say(channel=channel, thread_ts=thread, text=status_text(result.after, result.before))
        level = result.after["breaker"]
        if level in {"warning", "critical"} and (
            level != result.before["breaker"] or level != last_notified.get(key)
        ):
            say(channel=channel, text=f"Circuit breaker: {status_text(result.after)}")
        last_notified[key] = level

    last_notified: dict[str, str] = {}

    @app.command("/stira")
    def stira(ack, command, respond):
        ack()  # Must precede session construction and any work (Slack's 3s deadline).
        if command.get("text", "").strip().lower() != "status":
            respond(text="Usage: /stira status", response_type="ephemeral")
            return
        key = f"{command.get('team_id', '')}:{command['channel_id']}"
        respond(text=status_text(sessions.get(key).status()), response_type="ephemeral")


def main() -> int:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not token or not app_token:
        logger.warning("Slack skipped: set SLACK_BOT_TOKEN and SLACK_APP_TOKEN.")
        return 0
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        logger.warning(
            'Slack skipped: install the optional extra with pip install "stiracore[slack]".'
        )
        return 0
    app = App(token=token)
    register_handlers(app, ChatSessions("slack"))
    SocketModeHandler(app, app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
