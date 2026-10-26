"""Test helper: answer a host-confirmed operation as a person at the host would (ADR 0012
§8b). No dialog, no socket: the desk's decision, then the page's re-send with the header.
"""

from __future__ import annotations

import json

from stiracore.yolo import runtime
from stiracore.yolo.host_ops import HEADER


def host_decide(op_id: str, decision: str = "confirm") -> dict:
    desk = runtime.desk()
    assert desk is not None, "no host desk (is STIRA_YOLO_ALLOW set?)"
    return desk.decide(
        op_id,
        incarnation=desk.incarnation,
        decision=decision,
        principal="local:tester",
        channel="cli",
    )


def post(client, path, body, *, op_id=None, key=None):
    headers = {"Idempotency-Key": key or f"k-{id(body)}-{path}-{op_id}"}
    if op_id is not None:
        headers[HEADER] = op_id
    return client.post(
        path, data=json.dumps(body), content_type="application/json", headers=headers
    )


def confirmed_post(client, path, body, decision="confirm"):
    """POST; on 428 decide it on the host and send the same body again with the header."""
    response = post(client, path, body)
    data = response.get_json() or {}
    if response.status_code != 428 or data.get("code") != "host_confirmation_required":
        return response
    op_id = data["operation"]["id"]
    host_decide(op_id, decision)
    return post(client, path, body, op_id=op_id)
