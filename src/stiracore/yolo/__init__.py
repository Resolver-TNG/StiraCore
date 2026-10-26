"""YOLO mode (ADR 0012, #365) — phases 0 to 3 and 5a.

A confirmed request opens a window; while it is ``active`` the approver answers the Ask
of an **internal** tool in a human chat / Stage turn (phase 5a: :mod:`.tiers`,
:mod:`.origin`, ``YoloRequests.approve`` / ``consume``, pause / resume, the cap).
What exists:

* :mod:`.allowance` — the start-up allowance ``STIRA_YOLO_ALLOW`` and its caps (read once,
  invalid values refuse start-up) and the clock support check (§1, §7);
* :mod:`.state` — the request × window × latch state machine of §6b (pure, no I/O);
* :mod:`.audit_schema` — the ``audit.yolo.*`` row contract of §11 (allow-list validation);
* :mod:`.authority` — the shared grant boundary of §13 / #276 (``kind``, authority lock,
  monotonic epoch, single-use ``consume``), the minimum YOLO needs;
* :mod:`.own_dashboard` — the refusal of connections to the server's own dashboard (§8b);
* :mod:`.admin_guard` — the start-up-gated defences of the admin API: reserved ``yolo*``
  keys (§2) and the refusal of approvals / loosening saves while an agent tool call runs
  (one layer of defence, never a proof of a person, §8b);
* :mod:`.runtime` — the process-wide settings, read once;
* phase 2 (§8b): :mod:`.host_ops` (host-confirmed operations: one-time, bound, expiring),
  :mod:`.requests` (the YOLO request, its confirmation and — phase 3 — the window's
  open / close sequences, clocks, stop, ``/new`` and unlatch), :mod:`.window` (the
  marker, its durable write and the latch judgement),
  :mod:`.host_channel` (the fixed text, the command socket with kernel peer checks),
  :mod:`.host_dialog` (the macOS dialog + OS authentication), :mod:`.host_cli`
  (``stira yolo …`` / ``stira approvals …``).

With the allowance ``off`` (``STIRA_YOLO_ALLOW=off`` or ``"yolo": {"allow": "off"}``) none
of it is wired: no route, no hook, no guard, no host channel. Unset, the allowance is
``external`` (ADR 0012 addendum 2026-10-24: the button is shown; nothing is approved until a
person presses and confirms it).
"""
