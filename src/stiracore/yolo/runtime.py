"""Process-wide YOLO settings, read once (ADR 0012 §1), and the helpers routes use.

Nothing here changes behaviour while the allowance is ``off`` (``STIRA_YOLO_ALLOW=off`` or
``"yolo": {"allow": "off"}``; unset = ``external`` since the ADR 0012 addendum of
2026-10-24, which offers the button and approves nothing by itself): :func:`enabled` is False,
:func:`reserved_key_refusal`, :func:`tool_call_refusal` and :func:`host_gate` return
``None``, no route, hook, guard or host channel is wired.

Phase 2 (§8b): :func:`install_host` builds the confirmed operations, the YOLO request
and the confirmation channel once per process; :func:`host_gate` is the one check every
loosening human operation calls. Revision 6: the channel is the **browser** (a one-time
token and a checkbox in the dashboard, phase 2c) unless ``STIRA_YOLO_HOST_CONFIRM`` names a host
channel (command socket or desktop dialog), which is then required.

Phase 3 (§6, §11): a confirmed request opens the window (:mod:`.requests`), the marker
and the latch judgement live in the data directory (:mod:`.window`), :func:`stop` and
:func:`shutdown_host` end everything current, a watchdog thread re-checks the window's
clocks.

Phase 5a (§2 step 5, §6a): :data:`APPROVER` answers an internal tool's Ask while the
window is ``active`` (human chat / Stage turns only); the breaker pauses and resumes the
window; the cap ends it. Phase 5b: the button, the badge and the log line
(``yolo_ui.js``) draw only what :func:`status` answers. Phase 6 (§10): in a window of tier
external the listed external tools are answered too (secret-like arguments, MCP and exec:
a person; the external sub-cap; the destination only as a keyed hash in the row).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import host_dialog
from .admin_guard import (
    RESERVED_REFUSAL_CODE,
    TOOL_CALL_REFUSAL_CODE,
    ToolCallTracker,
    reserved_keys,
)
from .allowance import ClockReading, YoloSettings, clock_support, parse_settings
from .audit_schema import KNOWN_TOOLS
from .host_channel import HostChannelServer, HostDesk, choose_channel, gui_session, principal_for
from .host_ops import (
    CODES,
    HEADER,
    HostOperations,
    HostOpRefused,
    Unbindable,
    binding_digest,
    default_clock,
    target_digest,
)
from .requests import WATCHDOG_SEC, YoloRequests
from .window import WindowMarker

__all__ = [
    "APPROVER",
    "HEADER",
    "TRACKER",
    "advance_epoch",
    "apply_caps",
    "change_labels",
    "current",
    "current_window_id",
    "desk",
    "enabled",
    "host_gate",
    "install_host",
    "reserved_key_refusal",
    "revoke_operations",
    "settings",
    "shutdown_host",
    "status",
    "stop",
    "tool_call_refusal",
    "tool_label",
    "uninstall_host",
    "yolo_open",
]

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SETTINGS: YoloSettings | None = None
# One tracker per process: one process serves one agent (admin-api-permissions.md).
TRACKER = ToolCallTracker()


def settings(
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    default_allow: str | None = None,
) -> YoloSettings:
    """The settings of this process: parsed on the first call, never re-read (the caps
    alone may change later, for the next window: :func:`apply_caps`).

    ``config``: the cleaned ``yolo`` section of ``gateway-config.json`` (phase 7c; an
    environment variable wins per value). Raises ``YoloSettingError`` (the variable's
    name, never its value) for an invalid variable; the server entry point turns that
    into a refused start (exit 2). An invalid config value never refuses.

    ``default_allow``: the allowance when neither sets one (``None``: ``external``, ADR
    0012 addendum 2026-10-24; the server entry point passes ``off`` for a wildcard
    listener, so the default never refuses a start).
    """
    global _SETTINGS
    with _LOCK:
        if _SETTINGS is None:
            _SETTINGS = parse_settings(
                os.environ if environ is None else environ,
                config,
                **({} if default_allow is None else {"default_allow": default_allow}),
            )
        return _SETTINGS


def current() -> YoloSettings | None:
    """The settings parsed at start-up (``None`` before the first :func:`settings`)."""
    return _SETTINGS


def yolo_open() -> bool:
    """Phase 7c: a YOLO request, window, close or latch exists (a settings save is
    refused, ``yolo_busy``). Without the allowance (no desk): ``False``."""
    host = _DESK
    return bool(host is not None and host.yolo is not None and host.yolo.is_open())


def apply_caps(cap: int, external_cap: int, exec_cap: int) -> bool:
    """Phase 7c: a saved cap / sub-cap is used from the **next** window on.

    Refused (``False``, nothing changed) without the allowance, without a desk, or while a
    request or a window is open (``YoloRequests.set_caps`` checks that under its own lock:
    an open window keeps the caps it opened with). The allowance, the exec choice and the
    confirmation channel never change here (a restart).
    """
    global _SETTINGS
    host = _DESK
    with _LOCK:
        current_settings = _SETTINGS
    if current_settings is None or not current_settings.enabled or host is None:
        return False
    if host.yolo is None or not host.yolo.set_caps(cap, external_cap, exec_cap):
        return False
    with _LOCK:
        if _SETTINGS is current_settings:
            _SETTINGS = replace(
                current_settings, cap=cap, external_cap=external_cap, exec_cap=exec_cap
            )
    return True


def enabled() -> bool:
    current = _SETTINGS
    return current is not None and current.enabled


def _reset_for_tests(value: YoloSettings | None = None) -> None:
    global _SETTINGS
    uninstall_host()
    with _LOCK:
        _SETTINGS = value


def status() -> dict[str, Any]:
    """``GET /api/yolo/status`` (only routed with an allowance).

    ``offered`` (phase 5b): the button may be shown — allowed, the clocks supported, the
    confirmation channel available, not latched, no window and no current request;
    ``unavailable`` lists every reason it is not, the platform's clock support first.
    ``request`` (#597): an ``opened`` request only while its window is open, ``None`` once
    that window has ended; the other states as they are.
    Phase 3 adds (only adds): ``window`` (``None``, or its state ``active`` /
    ``paused`` / ``ended`` with the reason, tier, end time and remaining seconds),
    ``last_end`` (the last window end), ``latches`` and ``latch_reason``. Phase 5a adds
    to ``window``: ``used`` / ``reserved`` / ``limit`` (the cap's slots), ``paused``,
    ``pauses_last_hour``, ``resume_in_sec``, ``auto_approved`` and ``effective_tier``.
    Phase 5b adds ``auto_approvals`` (the log lines of the latest window: ``n``, ``tool``,
    ``ref``, ``limit``, ``tier``) — the page renders the button, the badge and the lines
    from this answer only. Phase 6 adds ``external_used`` / ``external_reserved`` /
    ``external_limit`` to ``window`` and ``external_n`` / ``external_limit`` to an external
    tool's line (never a destination). Phase 7 adds ``exec_offered``, ``caps.exec``, the
    window's ``exec`` / ``exec_used`` / ``exec_reserved`` / ``exec_limit`` /
    ``exec_effective`` and ``exec_n`` / ``exec_limit`` to an exec tool's line (never the
    command). Phase 7c adds ``settings`` (the values in use) and ``sources``.
    """
    current = _SETTINGS or YoloSettings()
    clocks = clock_support()
    host = _DESK
    request_state = host.yolo.status() if host is not None else None
    unavailable = []
    if not current.enabled:
        unavailable.append("not_allowed")
    if not clocks.supported:
        unavailable.append(clocks.reason)
    if host is None or not host.available:
        unavailable.append("host_channel_unavailable")
    if request_state is not None:
        request = request_state["request"]
        if request_state["latched"]:
            unavailable.append("yolo_latched")
        if request_state["window"] is not None:
            unavailable.append("yolo_active")
        if request is not None and request["state"] in ("pending", "issuing"):
            unavailable.append("yolo_request_pending")
    return {
        "allowance": current.allowance,
        "offered": not unavailable,
        "unavailable": unavailable,
        "clock_supported": clocks.supported,
        "caps": {
            "auto_approvals": current.cap,
            "external": current.external_cap,
            "exec": current.exec_cap,
        }
        if current.enabled
        else None,
        # Phase 7 (§9 option C): the tier dialog may offer "Include exec" — exec tools
        # exposed (``STIRA_ENABLE_EXEC_TOOLS``), the allowance ``external`` and (7c) the
        # config's ``offer_exec`` not false.
        "exec_offered": current.exec_offered,
        # Phase 7c (added): the values in use and where each came from (env / config /
        # default). The settings page reads ``GET /api/yolo/settings`` for the saved ones.
        "settings": {
            "allow": current.allowance,
            "offer_exec": current.offer_exec,
            "max_auto_approvals": current.cap,
            "max_external": current.external_cap,
            "max_exec": current.exec_cap,
            "host_confirm": current.host_confirm,
        },
        "sources": dict(current.sources),
        "window": request_state["window"] if request_state else None,
        "last_end": request_state["last_end"] if request_state else None,
        "request": request_state["request"] if request_state else None,
        "latched": bool(request_state and request_state["latched"]),
        "latch_reason": request_state["latch_reason"] if request_state else None,
        "latches": request_state["latches"] if request_state else [],
        "host_channel": host.channel if host is not None and host.available else None,
        "auto_approvals": request_state["auto_approvals"]
        if request_state
        else {"window": None, "items": []},
    }


def reserved_key_refusal(payload: Any) -> dict[str, Any] | None:
    """409 body for a save that names ``yolo*`` (only with an allowance)."""
    if not enabled():
        return None
    found = reserved_keys(payload)
    if not found:
        return None
    return {
        "error": "YOLO is not a setting; it cannot be saved or changed here.",
        "error_code": RESERVED_REFUSAL_CODE,
        "code": RESERVED_REFUSAL_CODE,
    }


def tool_call_refusal() -> dict[str, Any] | None:
    """409 body for a loosening admin operation while an agent tool call runs."""
    if not enabled() or TRACKER.running() == 0:
        return None
    return {
        "error": "An agent tool call is running; try again when it has finished.",
        "error_code": TOOL_CALL_REFUSAL_CODE,
        "code": TOOL_CALL_REFUSAL_CODE,
    }


# --- phase 2: host-confirmed operations (§8b) -------------------------------------------

_DESK: HostDesk | None = None
_SERVER: HostChannelServer | None = None
_WATCHDOG: threading.Event | None = None


def desk() -> HostDesk | None:
    return _DESK


def _dialog_runner(argv: list[str], timeout: float) -> tuple[int, str]:
    # Looked up at call time, so tests replace ``host_dialog.run_osascript`` (no dialog).
    return host_dialog.run_osascript(argv, timeout)


def install_host(
    *,
    data_dir: Path,
    record: Callable[..., str] | None,
    clock: Callable[[], ClockReading] | None = None,
    clocks_supported: bool | None = None,
    channel: str | None = None,
    ledger: Any = None,
    breaker: Callable[[], Any] | None = None,
    watchdog: bool = True,
    breaker_reasons: Callable[[], Any] | None = None,
    authority: Any = None,
) -> HostDesk | None:
    """Build the host side once (only with an allowance). ``record`` writes audit rows.

    Phase 3: ``ledger`` is the audit chain's read side (receipts, health, history), the
    window marker lives in ``data_dir``, ``breaker`` reads the live breaker level (a
    window confirmed while it is not NORMAL opens paused); a watchdog thread re-checks
    an open window's clocks every :data:`WATCHDOG_SEC` seconds (§7).

    The channel: ``STIRA_YOLO_HOST_CONFIRM`` (``auto`` → the desktop dialog with OS
    authentication on the person's macOS session, else the command). A command channel
    that cannot listen leaves the desk unavailable: every host-confirmed operation is
    then refused (``host_channel_unavailable``), never applied.

    ``authority`` (W1, #296): the process-wide :class:`stiracore.authority.AuthorityService`
    the window's permits use; its incarnation is the desk's. ``None``: a private boundary
    and a fresh incarnation (as before).
    """
    global _DESK, _SERVER
    current = _SETTINGS
    if current is None or not current.enabled:
        return None
    uninstall_host()
    if channel is None and not current.host_required:
        channel = "browser"
    if channel is None:
        ready = current.host_confirm != "command" and host_dialog.dialog_ready(_dialog_runner)
        channel = choose_channel(current.host_confirm, gui=gui_session(), dialog_ready=ready)
        if current.host_confirm == "dialog" and channel != "dialog":
            logger.warning(
                "STIRA_YOLO_HOST_CONFIRM=dialog is not available here; using the command"
            )
    _warn_exec(current.host_confirm, channel)
    incarnation = secrets.token_hex(16) if authority is None else authority.incarnation
    principal = principal_for(os.getuid()) if hasattr(os, "getuid") else "local:unknown"
    dialog: list[Callable[[dict], None]] = []

    def on_created(view: dict) -> None:
        for show in dialog:
            show(view)

    clock = clock or default_clock
    operations = HostOperations(
        incarnation=incarnation, clock=clock, channel=channel, on_created=on_created
    )
    yolo = YoloRequests(
        allowance=current.allowance,
        incarnation=incarnation,
        record=record,
        actor=principal,
        clocks_supported=clock_support().supported
        if clocks_supported is None
        else clocks_supported,
        clock=clock,
        on_created=on_created,
        channel=channel,
        ledger=ledger,
        marker=WindowMarker(data_dir) if ledger is not None else None,
        breaker=breaker,
        breaker_reasons=breaker_reasons,
        cap=current.cap,
        external_cap=current.external_cap,
        exec_offered=current.exec_offered,
        exec_cap=current.exec_cap,
        authority=authority,
    )
    host = HostDesk(operations, yolo)
    if watchdog:
        _start_watchdog(yolo)
    if channel == "browser":
        # No socket, no dialog: the dashboard confirms (routes: /confirm).
        with _LOCK:
            _DESK, _SERVER = host, None
        return host
    if channel == "dialog":
        dialog.append(
            host_dialog.DialogConfirmer(
                host.decide, incarnation=incarnation, principal=principal, runner=_dialog_runner
            )
        )
    try:
        server = HostChannelServer(host, data_dir, decisions=channel == "command")
    except (OSError, AttributeError) as error:
        logger.warning("YOLO host channel unavailable (%s)", type(error).__name__)
        server = None
        if channel == "command":
            host.available = False
    with _LOCK:
        _DESK, _SERVER = host, server
    return host


def _warn_exec(setting: str, channel: str) -> None:
    """§8b premise 1: with exec tools enabled, a command a person approved runs as the
    server's user and can confirm like a person on every **effective** channel except the
    OS-authenticated dialog that was actually selected (the setting alone guarantees
    nothing: ``dialog`` / ``auto`` fall back to the command where no dialog is available)."""
    from ..tools.tool_registry import exec_tools_enabled

    if channel == "dialog" or not exec_tools_enabled():
        return
    logger.warning(
        "YOLO: exec tools are enabled and the confirmation channel in use is %r "
        "(STIRA_YOLO_HOST_CONFIRM=%r); an approved command can confirm like a person. "
        "Only the OS-authenticated dialog, when it is actually selected, cannot be "
        "answered by a command.",
        channel,
        setting,
    )


def _start_watchdog(yolo: YoloRequests) -> None:
    global _WATCHDOG
    stop_event = threading.Event()

    def run() -> None:
        while not stop_event.wait(WATCHDOG_SEC):
            try:
                yolo.tick()
            except Exception:
                # Not swallowed: a watchdog that cannot check ends the window abnormally.
                logger.exception("YOLO watchdog check failed; ending the window")
                try:
                    yolo.fail_closed("revoked_clock_anomaly")
                except Exception:
                    logger.exception("YOLO watchdog could not end the window (latched)")

    with _LOCK:
        _WATCHDOG = stop_event
    threading.Thread(target=run, name="yolo-watchdog", daemon=True).start()


def stop() -> dict[str, Any]:
    """``POST /api/yolo/stop``: everything current ends (request, window, open confirmed
    operations). Never refused; without the allowance there is nothing to stop."""
    host = _DESK
    if host is not None:
        host.revoke("stop")
    return status()


def shutdown_host() -> None:
    """The server's stop: the window ends ``revoked(stop)`` (a normal end, the marker
    closes), then the host side is taken down."""
    host = _DESK
    if host is not None:
        try:
            host.revoke("stop", human=False)
        except Exception:
            logger.exception("YOLO stop at shutdown failed")
    uninstall_host()


def advance_epoch(reason: str = "tool_rules_saved") -> None:
    """The authority epoch moved (a rules save, …): the process-wide authority service
    advances it — with or without the YOLO desk (W1, #296) — and an issuing YOLO request
    is fenced."""
    from stiracore import authority

    # The YOLO request is fenced first (in memory, no I/O), then the service persists:
    # an issuance in progress never outlives a slow write (ADR 0012 §6b row 7).
    host = _DESK
    if host is not None and host.yolo is not None:
        host.yolo.advance_epoch()
    service = authority.current()
    if service is not None:
        service.narrow(reason)


def current_window_id() -> str | None:
    """#486 (ADR 0016 §7): the open window's id, or ``None`` (no desk, no window).

    ``delegate`` reads it when its child starts and while it runs: a different value
    (the window ended, or another one opened) stops the child."""
    host = _DESK
    yolo = host.yolo if host is not None else None
    if yolo is None:
        return None
    return yolo.open_window_id()


def window_facts() -> dict[str, Any] | None:
    """#508: the open window's facts that do not change while it is open — its id, tier,
    exec flag and fixed end — or ``None`` (no desk, no window, or it ended).

    For the agent's Tool Availability section: never a count, a pause or the time left, so
    the text changes only when a window opens or ends (#254). Never raises."""
    host = _DESK
    yolo = host.yolo if host is not None else None
    if yolo is None:
        return None
    try:
        window = yolo.status().get("window")
    except Exception:  # noqa: BLE001 - a status read never fails the turn
        return None
    if not isinstance(window, dict) or window.get("state") not in ("active", "paused"):
        return None
    return {
        "id": window.get("id"),
        "tier": window.get("tier"),
        "exec": bool(window.get("exec")),
        "ends_at": window.get("ends_at"),
    }


def revoke_operations(reason: str) -> int:
    """``/new`` and stop (revision 6): every open confirmed operation ends and the YOLO
    request is fenced. Without the allowance (no desk): nothing, ``0``."""
    host = _DESK
    if host is None:
        return 0
    return host.revoke(reason)


class AutoApprover:
    """The gate's handle on the YOLO approver (ADR 0012 §2 step 5, phase 5a).

    ``CheckpointHooks.set_auto_approver(APPROVER)`` is called only by a server started
    with ``STIRA_YOLO_ALLOW``. Each call looks the current desk up, so a server without
    a desk (or after its stop) answers nothing: every Ask goes to a person.
    """

    @staticmethod
    def _yolo():
        host = _DESK
        return None if host is None else host.yolo

    def approve(self, **kwargs: Any):
        yolo = self._yolo()
        if yolo is None:
            return None, "no_window"
        return yolo.approve(**kwargs)

    def consume(self, grant: Any):
        yolo = self._yolo()
        if yolo is None:
            return False, "window_ended"
        return yolo.consume(grant)

    def void(self, grant: Any, reason: str = "binding_mismatch") -> bool:
        yolo = self._yolo()
        return False if yolo is None else yolo.void(grant, reason)

    # #448: what ran in the window (counts only; nothing without a desk or a window).
    def record_call(self, tool_name: Any, decision: Any) -> str | None:
        yolo = self._yolo()
        return None if yolo is None else yolo.record_call(tool_name, decision)

    def record_failure(self, window_id: Any) -> None:
        yolo = self._yolo()
        if yolo is not None:
            yolo.record_failure(window_id)

    def record_turn(self) -> None:
        yolo = self._yolo()
        if yolo is not None:
            yolo.record_turn()


APPROVER = AutoApprover()


def uninstall_host() -> None:
    global _DESK, _SERVER, _WATCHDOG
    with _LOCK:
        server, _DESK, _SERVER = _SERVER, None, None
        watchdog, _WATCHDOG = _WATCHDOG, None
    if watchdog is not None:
        watchdog.set()
    if server is not None:
        server.close()


_MESSAGES = {
    "host_confirmation_required": "This needs a confirmation first; nothing was applied yet.",
    "stale_operation": "What was confirmed has changed since; nothing was applied.",
    "host_operation_expired": "The host confirmation expired; nothing was applied.",
    "host_operation_denied": "The operation was denied on the host; nothing was applied.",
    "host_operation_cancelled": "The operation was cancelled; nothing was applied.",
    "host_operation_unknown": "No such host operation (it ended, or the server restarted).",
    "host_operation_pending": "Not confirmed on the host yet; nothing was applied.",
    "host_operation_used": "This host confirmation was already used.",
    "host_operation_mismatch": "The request does not match what was confirmed.",
    "host_rate_limited": "Another host confirmation is open, or too many were requested.",
    "host_channel_unavailable": "The host confirmation is unavailable; nothing was applied.",
    "host_not_acknowledged": "The confirmation box was not ticked; nothing was confirmed.",
    "host_browser_refused": "Only the dashboard page in a browser can confirm this.",
    "host_confirm_on_host": "This server requires the confirmation on the host.",
    "host_operation_incarnation": "The server restarted since; nothing was applied.",
    "yolo_not_latched": "That latch does not exist (already unlatched?).",
    "yolo_busy": "A YOLO request or window is open or closing; try again when it ended.",
    "yolo_audit_failed": "The audit row could not be written; nothing was changed.",
}


def host_refusal(code: str) -> tuple[dict[str, Any], int]:
    body = {
        "error": _MESSAGES.get(code, "Refused."),
        "error_code": code,
        "code": code,
        "errors": [],
    }
    return body, CODES.get(code, 409)


def tool_label(name: Any) -> str:
    """A registry tool name in clear; anything else (MCP, dynamic) as a short digest."""
    if type(name) is str and name in KNOWN_TOOLS:
        return name
    text = name if type(name) is str else repr(type(name))
    return "mcp:" + target_digest(text)[:8]


_CODE = re.compile(r"[a-z0-9_]{1,40}")


def change_labels(reasons: Any) -> list[str]:
    """Server-side reason / danger codes for the host text (codes only; at most 8)."""
    out: list[str] = []
    for reason in reasons if isinstance(reasons, (list, tuple)) else []:
        label = reason if type(reason) is str and _CODE.fullmatch(reason) else "other"
        if label not in out:
            out.append(label)
    return out[:8]


def host_gate(
    kind: str,
    *,
    route: str,
    body: Any,
    target: Any,
    summary: dict,
    header: Any,
) -> tuple[dict[str, Any], int] | None:
    """``None`` → proceed (no allowance, or a confirmed, matching, fresh operation, now
    consumed). Otherwise the refusal body and status: 428 ``host_confirmation_required``
    with the new operation, or why the named operation does not apply."""
    if not enabled():
        return None
    host = _DESK
    if host is None or not host.available:
        return host_refusal("host_channel_unavailable")
    try:
        binding = binding_digest(kind, route, body)
    except Unbindable:
        return host_refusal("host_operation_mismatch")
    generation = target_digest(target)
    if header is None or header == "":
        try:
            view = host.operations.request(
                kind, binding=binding, generation=generation, summary=summary
            )
        except HostOpRefused as error:
            return host_refusal(error.code)
        refusal, status_code = host_refusal("host_confirmation_required")
        command = (
            f"stira approvals decide {view['short']}" if view["channel"] == "command" else None
        )
        refusal["operation"] = {
            key: view[key]
            for key in ("id", "short", "kind", "code", "expires_in", "channel", "word", "instance")
        }
        refusal["operation"]["command"] = command
        if view["channel"] == "browser":
            # What the page shows in its fixed format (enums / short codes, validated by
            # host_ops), and the one-time token (only in this answer).
            refusal["operation"]["summary"] = view["summary"]
            refusal["operation"]["confirm_token"] = view["confirm_token"]
        return refusal, status_code
    try:
        host.operations.consume(header, kind=kind, binding=binding, generation=generation)
    except HostOpRefused as error:
        return host_refusal(error.code)
    return None
