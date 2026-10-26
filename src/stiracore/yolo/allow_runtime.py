"""The server's allowance service and what the page reads (ADR 0025 PR 2).

One :class:`.allow_service.AllowanceService` per server process (:func:`install`, called
by ``dashboard/stira_server.py`` with the audit chain, the live breaker, the rule reader
and the agent's path policy). Everything here is memory only, like the table itself.

* :func:`card_view` — the ``allow_again`` part of a card's pending view: whether the two
  buttons are offered, the class text in clear (page only, never a row) and the one-time
  view token; or the reason code of the line 「この操作は毎回確認します(…)」.
* :func:`status` — ``GET /api/allowances``: the live allowances of this session (tool,
  class, time left, uses) and the recent automatic answers (the chat's log lines).
* :func:`note_use` — one automatic answer (called by the gate after the consume CAS).

Until PR 3 nothing offers the buttons (``session_allow.ENABLED`` is False): every view is
``{"offered": False, "reason": "not_enabled"}`` and the list is always empty. Revoke and
"Revoke all" are narrowings and work either way.
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .allow_class import ORIGIN_CHAT
from .session_allow import ENABLED, MAX_PER_SESSION, NOT_ENABLED, table

__all__ = [
    "RECENT_KEPT",
    "card_origin",
    "card_view",
    "class_label",
    "install",
    "note_use",
    "service",
    "status",
]

RECENT_KEPT = 20
_LOCK = threading.Lock()
_SERVICE: Any = None
_POLICY: Any = None
_RECENT: deque = deque(maxlen=RECENT_KEPT)
_SEQ = 0
# request id -> the last view handed out (its clear token is reused while it is live, so
# two tabs polling the same card do not void each other's token).
_VIEWS: dict[str, dict] = {}


def install(service: Any, *, policy: Any = None) -> None:
    """Connect the process's service (``None`` disconnects; tests)."""
    global _SERVICE, _POLICY
    with _LOCK:
        _SERVICE, _POLICY = service, policy


def service() -> Any:
    with _LOCK:
        return _SERVICE


def policy() -> Any:
    with _LOCK:
        return _POLICY


def enabled() -> bool:
    current = service()
    return current is not None and getattr(current, "enabled", False) is True and ENABLED


def _reset_for_tests() -> None:
    global _SEQ
    install(None)
    with _LOCK:
        _RECENT.clear()
        _VIEWS.clear()
        _SEQ = 0


def card_ended(request_id: Any, status: Any) -> None:
    """The broker resolved a card (approved, rejected, expired, closed …): its view token
    is dead; a card a button was pressed on that ended without running is a terminal
    result for Make Always (§9: ``expired`` / ``cancelled``). Memory only; never raises."""
    with contextlib.suppress(Exception):
        with _LOCK:
            _VIEWS.pop(request_id, None)
        table().void_view(request_id)
        current = service()
        if current is not None and status in ("expired", "closed", "cancelled"):
            current.terminal(request_id, "void")


def card_origin(agent_id: Any = None) -> str | None:
    """At request time: ``"chat_turn:<session epoch>"`` for a person's chat / Stage turn of
    this session, else ``None`` (a job, a resume, a webhook, a sub-agent; ADR 0025 §2)."""
    from . import origin

    try:
        if origin.covered(agent_id) and origin.started_by_person():
            return f"{ORIGIN_CHAT}:{table().session_epoch}"
    except Exception:  # noqa: BLE001 - an unreadable origin is never covered
        return None
    return None


def _origin_now(stored: Any) -> str | None:
    """The stored request's origin, if it is a chat turn of the **current** session."""
    if type(stored) is not str or not stored.startswith(ORIGIN_CHAT + ":"):
        return None
    try:
        epoch = int(stored.split(":", 1)[1])
    except ValueError:
        return None
    return ORIGIN_CHAT if epoch == table().session_epoch else None


def origin_of(item: dict) -> str | None:
    """The press: the stored request's origin, if it is a chat turn of this session."""
    return _origin_now(item.get("allow_origin"))


def class_label(class_kind: str, class_key: str, path_policy: Any = None) -> str:
    """The class in clear for the button and the list (page only): a directory relative to
    the write root that contains it (``projects/game/``; ``""`` for the whole root), the
    host, the layer or the tool."""
    if class_kind != "dir_subtree":
        return class_key
    path_policy = path_policy if path_policy is not None else policy()
    roots = list(getattr(path_policy, "write_roots", None) or [])
    workspace = getattr(path_policy, "workspace", None)
    if workspace is not None:
        roots.append(workspace)
    target = Path(class_key)
    best: Path | None = None
    for root in roots:
        with contextlib.suppress(Exception):
            base = Path(os.path.realpath(os.fspath(root)))
            if (target == base or base in target.parents) and (
                best is None or len(base.parts) > len(best.parts)
            ):
                best = base
    if best is None:
        return target.name + "/"
    relative = target.relative_to(best).as_posix()
    return "" if relative in ("", ".") else relative + "/"


def card_view(item: dict) -> dict:
    """The ``allow_again`` view of one pending card (a copy of the broker's entry)."""
    current = service()
    if current is None or not enabled():
        return {"offered": False, "reason": NOT_ENABLED}
    if item.get("restored"):
        return {"offered": False, "reason": "not_this_session"}
    if item.get("supersedes"):
        # An edited call is not "the same" (§3): approve once only.
        return {"offered": False, "reason": "edited"}
    if item.get("reason_code") != "rule_ask":
        return {"offered": False, "reason": "breaker"}
    origin = _origin_now(item.get("allow_origin"))
    if origin is None:
        return {"offered": False, "reason": "not_this_session"}
    with _LOCK:
        cached = _VIEWS.get(item["id"])
    if cached is not None and cached.get("args_hash") == item.get("args_hash"):
        try:
            generation = current.evidence(item.get("tool_name"))
        except Exception:  # noqa: BLE001
            generation = None
        if generation is not None and current.table.view_live(
            item["id"], cached["view"]["token"], rule_generation=generation.rule_generation
        ):
            return _with_list_full(current, dict(cached["view"]))
    try:
        view = current.offer(
            request_id=item["id"],
            args_hash=item["args_hash"],
            tool=item.get("tool_name"),
            tool_input=item.get("tool_input"),
            policy=policy(),
            origin=origin,
        )
    except Exception:  # noqa: BLE001 - a view that cannot be computed offers nothing
        return {"offered": False, "reason": "read_failed"}
    if not view.get("offered"):
        return {"offered": False, "reason": str(view.get("reason") or "unclassified")}
    out = {
        "offered": True,
        "class_kind": view["class_kind"],
        "label": class_label(view["class_kind"], view["class_key"]),
        "whole_root": bool(view.get("whole_root")),
        "token": view["token"],
        "max_hours": int(current.table.duration_sec // 3600) or 1,
    }
    with _LOCK:
        _VIEWS[item["id"]] = {"args_hash": item.get("args_hash"), "view": dict(out)}
        while len(_VIEWS) > 200:
            _VIEWS.pop(next(iter(_VIEWS)))
    return _with_list_full(current, out)


def _with_list_full(current: Any, view: dict) -> dict:
    """At 20 live allowances the button is shown disabled (§3 ``list_full``)."""
    view.pop("list_full", None)
    if len(current.table.active()) >= MAX_PER_SESSION:
        view["list_full"] = True
    return view


def note_use(record: Any) -> None:
    """One automatic answer of an allowance (after the consume CAS): a log line."""
    global _SEQ
    with _LOCK:
        _SEQ += 1
        _RECENT.append(
            {
                "seq": _SEQ,
                "ref": str(record.allow_id)[:8],
                "tool": record.target.tool,
                "class_kind": record.target.class_kind,
            }
        )


def status() -> dict:
    """``GET /api/allowances``: the live list of this session and the recent answers."""
    import time

    from .host_ops import elapsed

    current = service()
    active = []
    if current is not None:
        try:
            reading = current.table._clock()  # display only (the deadline is the CAS's)
        except Exception:  # noqa: BLE001
            reading = None
        for record in current.table.active():
            try:
                left = current.table.duration_sec - elapsed(record.issued_at, reading)
            except Exception:  # noqa: BLE001 - unknown: shown as nothing left
                left = 0
            active.append(
                {
                    "allow_id": record.allow_id,
                    "ref": record.allow_id[:8],
                    "tool": record.target.tool,
                    "class_kind": record.target.class_kind,
                    "label": class_label(record.target.class_kind, record.target.class_key),
                    "whole_root": bool(record.target.whole_root),
                    "expires_at": record.expires_at,
                    "remaining_sec": max(0, int(left)),
                    "uses": int(record.uses),
                }
            )
    with _LOCK:
        recent = [dict(item) for item in _RECENT]
        seq = _SEQ
    return {
        "enabled": enabled(),
        "audit_failed": bool(current is not None and current.table.audit_failed),
        "max": MAX_PER_SESSION,
        "allowances": active,
        "uses": recent,
        "seq": seq,
        "server_time": time.time(),
    }
