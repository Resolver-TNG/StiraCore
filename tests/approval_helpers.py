"""Test helpers: a person approves every approval card while a test sends (#547).

Kept out of ``tests/test_http_request_consent.py`` (which skips without the upstream
``http_request``) so a module that imports them is never skipped as a whole.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager


@contextmanager
def approving_cards(client, token: str):
    """While inside: a person approves every approval card at once (another thread).

    For tests whose subject is a defence *behind* the approval (#547: a modifying
    ``http_request`` runs only after a person approved it; ``BYPASS_TOOL_CONSENT`` no
    longer skips that). Yields the list of the cards it approved.
    """
    approved: list[dict] = []
    done = threading.Event()

    def run() -> None:
        while not done.is_set():
            for item in client.get("/api/hitl/pending").get_json().get("requests", []):
                answer = client.post(
                    f"/api/hitl/{item['id']}/decision",
                    json={"action": "approve", "args_hash": item.get("args_hash")},
                    headers={"X-CSRF-Token": token, "Idempotency-Key": f"ok-{item['id']}"},
                )
                if answer.status_code in (200, 202):
                    approved.append(item)
            done.wait(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield approved
    finally:
        done.set()
        thread.join(5)


def set_rule(client, name: str, policy: str, post=None):
    """Save one tool rule (``post``: a caller's confirmed POST helper, else the client)."""
    body = {"name": name, "enabled": True, "policy": policy, "confirm": True}
    if post is not None:
        return post(client, "/api/config/tools", body)
    return client.post("/api/config/tools", json=body)
