"""The YOLO values on the settings page (ADR 0012 phase 7c).

* ``GET /api/yolo/settings`` — every value: what is saved (the ``yolo`` section of
  ``gateway-config.json``), what a start would use and from where (``env`` / ``config``
  / ``default``), what this process uses now, its default, the variable that pins it,
  when a save is used (``restart`` / ``next_window``). It changes nothing.
* ``POST /api/yolo/settings {values, base_version[, confirm, acknowledged]}`` — a person's
  save of some values (``null`` removes one: its default applies).

Both exist **with and without** the allowance (that is what the page enables YOLO
from); neither draws a button, a badge or a poll, and without the allowance nothing at
run time reads the saved values before the next start.

Who can save — the reserved-key rule of §2 stays: ``/api/config/*``, ``/api/tools/*`` and
``/api/scheduler/*`` still refuse every ``yolo*`` key, and this route is the only writer of
the section. A save is refused (nothing written):

* while a chat turn runs (``turn_running``) or an agent tool call runs
  (``tool_call_running``) — the agent's tools only run inside a turn, so its own HTTP
  tools cannot save here (the same hold as ``/api/chat/session/new``);
* while a YOLO request or window is open, or a latch is set (``yolo_busy``) — an open
  window is never changed, not even narrowed (simple first);
* for a value its environment variable pins (``pinned_by_env``), a value out of the
  variables' ranges (field errors, nothing rounded), a stale ``base_version``
  (``version_conflict``), an allowance on a wildcard listener (``wildcard_bind``: the
  server would refuse to start).

A save that **loosens** (a higher allowance, ``offer_exec`` turned on, a higher cap or
sub-cap, a host channel replaced by the browser) is a confirmed operation: without the
allowance the page's confirmation (``confirm: true`` and ``acknowledged`` naming exactly
the loosening codes, else 409 ``needs_confirmation``); with it the host gate of §8b
(kind ``yolo_settings``: 428, then the browser checkbox or the host). Narrowing saves
apply directly. A confirmed loosening writes ``audit.yolo.settings_saved`` (field names
only, never a value) to the audit chain **before** the file; when that row cannot be
written the save is refused (503 ``audit_failed``, nothing written). The settings history
(``settings-history.jsonl``, the rollback) is not written: one record, in the audit.

When it is used: the allowance, ``offer_exec`` and the confirmation channel at the next
start (restart); the caps from the next window (an open window keeps its caps).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from flask import Flask, jsonify, request

from ..tools.tool_registry import EXEC_ENV_VAR, exec_tools_enabled
from . import runtime
from .allowance import (
    CAP_MAX,
    HOST_CONFIRM_VALUES,
    YoloSettingError,
    YoloSettings,
    pinned_by_env,
    resolve_values,
    tier_rank,
)
from .audit_schema import NO_WINDOW, SCHEMA
from .config_file import APPLIES, DEFAULTS, ENV_OF, FIELDS, LOW, clean, validate
from .host_channel import principal_for

__all__ = ["KIND", "ROUTE", "describe", "install", "loosenings"]

logger = logging.getLogger(__name__)

ROUTE = "/api/yolo/settings"
KIND = "yolo_settings"
_BODY_KEYS = frozenset({"values", "base_version", "confirm", "acknowledged"})
# Stricter → looser: a lower rank is looser (the browser needs no host).
_CHANNEL_RANK = {"browser": 0, "command": 1, "auto": 1, "dialog": 2}
# Loosening code → the field it names (the audit row carries field names only).
_CODE_FIELD = {
    "yolo_allow": "allow",
    "yolo_offer_exec": "offer_exec",
    "yolo_cap": "max_auto_approvals",
    "yolo_external_cap": "max_external",
    "yolo_exec_cap": "max_exec",
    "yolo_host_confirm": "host_confirm",
}


def _version(section: Mapping[str, Any]) -> str:
    text = json.dumps(section, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _running_values(current: YoloSettings) -> dict[str, Any]:
    return {
        "allow": current.allowance,
        "offer_exec": current.offer_exec,
        "max_auto_approvals": current.cap,
        "max_external": current.external_cap,
        "max_exec": current.exec_cap,
        "host_confirm": current.host_confirm,
    }


def loosenings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Loosening codes of ``before → after`` (resolved values, :data:`FIELDS`)."""
    codes = []
    if tier_rank(after["allow"]) > tier_rank(before["allow"]):
        codes.append("yolo_allow")
    if after["offer_exec"] is True and before["offer_exec"] is not True:
        codes.append("yolo_offer_exec")
    for name, code in (
        ("max_auto_approvals", "yolo_cap"),
        ("max_external", "yolo_external_cap"),
        ("max_exec", "yolo_exec_cap"),
    ):
        if after[name] > before[name]:
            codes.append(code)
    if _CHANNEL_RANK[after["host_confirm"]] < _CHANNEL_RANK[before["host_confirm"]]:
        codes.append("yolo_host_confirm")
    return codes


def _defaults(wildcard_bind: bool) -> dict[str, Any]:
    """The defaults a start would use: the allowance default is ``off`` on a wildcard
    listener (ADR 0012 addendum 2026-10-24: a default never refuses a start)."""
    return {**DEFAULTS, "allow": "off"} if wildcard_bind else dict(DEFAULTS)


def describe(
    raw_section: Any,
    *,
    environ: Mapping[str, str],
    wildcard_bind: bool,
) -> dict[str, Any]:
    """The ``GET`` answer for the section as stored (``raw_section``)."""
    section, warnings = clean(raw_section)
    defaults = _defaults(wildcard_bind)
    try:
        values, sources, more = resolve_values(environ, section, defaults["allow"])
        start_error = None
    except YoloSettingError:
        # Not reachable for a server that started (its variables were valid then).
        values, sources, more = dict(defaults), dict.fromkeys(FIELDS, "default"), []
        start_error = "env_invalid"
    current = runtime.current() or YoloSettings()
    running = _running_values(current)
    pinned = pinned_by_env(environ)
    fields = {}
    for name in FIELDS:
        entry = {
            "saved": section.get(name),
            "value": values[name],
            "source": sources[name],
            "running": running[name],
            "default": defaults[name],
            "env": ENV_OF[name],
            "pinned": pinned[name],
            "applies": APPLIES[name],
            "pending_restart": APPLIES[name] == "restart" and running[name] != values[name],
        }
        if name in LOW:
            entry.update(min=LOW[name], max=CAP_MAX)
        fields[name] = entry
    return {
        "fields": fields,
        "version": _version(section),
        "enabled": current.enabled,
        "exec_tools": exec_tools_enabled(),
        "exec_tools_env": EXEC_ENV_VAR,
        "busy": runtime.yolo_open(),
        "wildcard_bind": wildcard_bind,
        "restart_pending": [name for name in FIELDS if fields[name]["pending_restart"]],
        "warnings": sorted(set(warnings) | set(more)),
        "start_error": start_error,
        "file": "gateway-config.json",
        "choices": {"allow": ["off", "internal", "external"], "host_confirm": HOST_CONFIRM_VALUES},
    }


def _refuse(code: str, status: int, **extra: Any):
    return jsonify({"error": code, "error_code": code, "code": code, "errors": [], **extra}), status


def install(
    app: Flask,
    *,
    config_store: Any,
    turn_running: Callable[[], bool],
    wildcard_bind: Callable[[], bool],
    record: Callable[..., str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Install both routes (with or without the allowance).

    ``record(event_type, **fields)`` writes one ``audit.yolo.*`` row
    (``AuditChain.record_yolo``); without it a loosening save is refused (``audit_failed``).
    """
    env = os.environ if environ is None else environ
    incarnation_ref = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    principal = principal_for(os.getuid()) if hasattr(os, "getuid") else "local:unknown"

    def audit_loosening(codes: list[str], changed: list[str]) -> bool:
        """``audit.yolo.settings_saved`` before the write; ``False`` = not written."""
        if record is None:
            return False
        operation_id = secrets.token_hex(16)
        try:
            event_id = record(
                "audit.yolo.settings_saved",
                schema=SCHEMA,
                operation_id=operation_id,
                phase="decision",
                event_id=f"op:{operation_id}:decision",
                actor_kind="human",
                actor=principal,
                surface="admin",
                incarnation_ref=incarnation_ref,
                key_id="settings",
                delta_class="loosening",
                window_id=NO_WINDOW,
                generation_from=0,
                generation_to=0,
                authority_epoch=0,
                fields=sorted({_CODE_FIELD[code] for code in codes}),
                changed=sorted(set(changed)),
            )
        except Exception:  # noqa: BLE001 - any failure is an audit-write failure
            return False
        return type(event_id) is str and bool(event_id)

    def load_raw():
        gateway, _agent = config_store.load()
        return gateway, gateway.yolo

    def busy_refusal():
        if turn_running():
            return _refuse("turn_running", 409)
        if runtime.TRACKER.running():
            return _refuse("tool_call_running", 409)
        if runtime.yolo_open():
            return _refuse("yolo_busy", 409)
        return None

    @app.get(ROUTE)
    def yolo_settings_get():
        with config_store.lock:
            try:
                _gateway, raw = load_raw()
            except (OSError, ValueError, TypeError):
                return _refuse("config_unavailable", 503)
            return jsonify(describe(raw, environ=env, wildcard_bind=wildcard_bind()))

    @app.post(ROUTE)
    def yolo_settings_post():
        data = request.get_json(silent=True)
        if type(data) is not dict or not set(data) <= _BODY_KEYS:
            return _refuse("invalid_request", 400)
        changes = data.get("values")
        if type(changes) is not dict or not changes or not set(changes) <= set(FIELDS):
            return _refuse("invalid_request", 400)
        refused = busy_refusal()
        if refused is not None:
            return refused
        with config_store.lock:
            try:
                gateway, raw = load_raw()
            except (OSError, ValueError, TypeError):
                return _refuse("config_unavailable", 503)
            section, _warnings = clean(raw)
            if data.get("base_version") != _version(section):
                return _refuse("version_conflict", 409)
            pinned = pinned_by_env(env)
            errors = [
                {"field": name, "code": "pinned_by_env", "params": {"env": ENV_OF[name]}}
                for name in changes
                if pinned[name]
            ]
            if errors:
                return _refuse("invalid", 400, errors=errors)
            candidate = dict(section)
            for name, value in changes.items():
                if value is None:
                    candidate.pop(name, None)
                else:
                    candidate[name] = value
            default_allow = _defaults(wildcard_bind())["allow"]
            try:
                before, _s, _w = resolve_values(env, section, default_allow)
                # The sub-caps are checked against the cap in use after the save (a
                # variable's value when the cap is pinned).
                cap = (
                    before["max_auto_approvals"]
                    if pinned["max_auto_approvals"]
                    else candidate.get("max_auto_approvals", DEFAULTS["max_auto_approvals"])
                )
                errors = validate(candidate, cap if type(cap) is int else None)
                if errors:
                    return _refuse("invalid", 400, errors=errors)
                after, _s, _w = resolve_values(env, candidate, default_allow)
            except YoloSettingError:
                return _refuse("env_invalid", 409)
            if after["allow"] != "off" and wildcard_bind():
                errors = [{"field": "allow", "code": "wildcard_bind", "params": {}}]
                return _refuse("invalid", 400, errors=errors)
            env_caps = [ENV_OF[n] for n in FIELDS if n != "allow" and pinned[n]]
            if after["allow"] == "off" and env_caps:
                # The next start would refuse: a YOLO variable set without the allowance.
                errors = [
                    {"field": "allow", "code": "env_needs_allow", "params": {"env": env_caps}}
                ]
                return _refuse("invalid", 400, errors=errors)
            codes = loosenings(before, after)
            if codes:
                if runtime.enabled():
                    gate = runtime.host_gate(
                        KIND,
                        route=ROUTE,
                        body=data,
                        target={"yolo": section},
                        summary={"action": KIND, "changes": codes},
                        header=request.headers.get(runtime.HEADER),
                    )
                    if gate is not None:
                        return jsonify(gate[0]), gate[1]
                else:
                    acknowledged = data.get("acknowledged")
                    if (
                        data.get("confirm") is not True
                        or type(acknowledged) is not list
                        or sorted({a for a in acknowledged if type(a) is str}) != sorted(codes)
                        or len(acknowledged) != len(codes)
                    ):
                        return _refuse("needs_confirmation", 409, needs_confirmation=codes)
            # Read again under the lock, right before the write (a turn may have started).
            refused = busy_refusal()
            if refused is not None:
                return refused
            changed = [name for name in FIELDS if before[name] != after[name]]
            if codes and not audit_loosening(codes, changed):
                # ADR 0013 §4: a loosening whose audit row cannot be written does not happen.
                return _refuse("audit_failed", 503)
            gateway.yolo = candidate
            try:
                gateway.save()
            except OSError:
                return _refuse("save_failed", 500)
            if codes:
                logger.warning(
                    "YOLO settings loosened on the settings page (%s); the allowance, the "
                    "exec choice and the channel are used at the next start, caps from the "
                    "next window",
                    ", ".join(codes),
                )
            caps_applied = None
            caps = ("max_auto_approvals", "max_external", "max_exec")
            if runtime.enabled() and any(before[n] != after[n] for n in caps):
                caps_applied = runtime.apply_caps(
                    after["max_auto_approvals"], after["max_external"], after["max_exec"]
                )
            body = describe(candidate, environ=env, wildcard_bind=wildcard_bind())
            body.update(
                saved=True,
                changed=changed,
                loosened=codes,
                caps_applied=caps_applied,
            )
            return jsonify(body)
