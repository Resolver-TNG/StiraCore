"""The ``yolo`` section of ``gateway-config.json`` (ADR 0012 phase 7c).

Operator request 2026-10-19: the YOLO values may come from the config file and be changed
on the settings page, not only from environment variables. The precedence is per value:
**environment > config > default** (:func:`.allowance.resolve_values`). The section::

    "yolo": {
      "allow": "off" | "internal" | "external",   # STIRA_YOLO_ALLOW; default external
      "offer_exec": true,                          # no variable; "Include exec" offered
      "max_auto_approvals": 2000,                  # STIRA_YOLO_MAX_AUTO_APPROVALS
      "max_external": 200,                         # STIRA_YOLO_MAX_EXTERNAL
      "max_exec": 200,                             # STIRA_YOLO_MAX_EXEC
      "host_confirm": "browser"                    # STIRA_YOLO_HOST_CONFIRM
    }

Two readings of the same values:

* :func:`clean` — the file as a person may have hand-edited it, read at start-up and by
  the settings page: never refuses. An unknown or invalid ``allow`` is ``off`` (the safe
  side); an integer out of range is rounded into it; anything else not understood is left
  out (its default applies). Each problem is one warning code.
* :func:`validate` — a save from the settings page: every value exact, or a field error
  (nothing is rounded silently on a save).

Only built-in values are evaluated (``type(x) is int`` / ``str`` / ``bool``): a ``bool``
is never a number, a ``float`` never an integer.

Who writes it: only ``POST /api/yolo/settings`` (:mod:`.settings_api`) — a person's
confirmed save (a loosening one recorded as ``audit.yolo.settings_saved``). Every other
save path keeps refusing ``yolo*`` keys (§2), the hub's gateway saves carry the section
over unchanged, and the agent's file tools (``file_write``, ``editor``) cannot write the
configuration files, approved or not: the store's files are ``read_only_paths`` of the
write floor wherever they are, and with ``STIRA_DATA_DIR`` also inside the floor tree
(``tests/test_yolo_config_floor.py``: with and without ``STIRA_DATA_DIR``, through a
symlink and ``..``). Not covered, as accepted in ADR 0012 §9: a shell command
auto-approved in an exec window can edit this file like the tool rules file; the next
start names the fields taken from it (an ``INFO`` line, no values) and the settings page
and the status show each value's source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .allowance import (
    ALLOW_ENV,
    CAP_ENV,
    CAP_MAX,
    DEFAULT_ALLOW,
    DEFAULT_CAP,
    DEFAULT_EXEC_CAP,
    DEFAULT_EXTERNAL_CAP,
    EXEC_CAP_ENV,
    EXTERNAL_CAP_ENV,
    HOST_CONFIRM_ENV,
    HOST_CONFIRM_VALUES,
    TIERS,
)

__all__ = [
    "APPLIES",
    "DEFAULTS",
    "ENV_OF",
    "FIELDS",
    "LOW",
    "SECTION",
    "clean",
    "validate",
]

SECTION = "yolo"
FIELDS = (
    "allow",
    "offer_exec",
    "max_auto_approvals",
    "max_external",
    "max_exec",
    "host_confirm",
)
ENV_OF: dict[str, str | None] = {
    "allow": ALLOW_ENV,
    "offer_exec": None,
    "max_auto_approvals": CAP_ENV,
    "max_external": EXTERNAL_CAP_ENV,
    "max_exec": EXEC_CAP_ENV,
    "host_confirm": HOST_CONFIRM_ENV,
}
DEFAULTS: dict[str, Any] = {
    "allow": DEFAULT_ALLOW,  # ADR 0012 addendum 2026-10-24: offered, nothing approved
    "offer_exec": True,
    "max_auto_approvals": DEFAULT_CAP,
    "max_external": DEFAULT_EXTERNAL_CAP,
    "max_exec": DEFAULT_EXEC_CAP,
    "host_confirm": "browser",
}
# When a saved value is used: the allowance, the exec choice and the confirmation channel
# are read once at start-up (the routes, the guard, the host channel are built from them);
# the caps are copied into each window when it opens.
APPLIES = {
    "allow": "restart",
    "offer_exec": "restart",
    "max_auto_approvals": "next_window",
    "max_external": "next_window",
    "max_exec": "next_window",
    "host_confirm": "restart",
}
LOW = {"max_auto_approvals": 1, "max_external": 0, "max_exec": 0}
_CHOICES = {"allow": TIERS, "host_confirm": HOST_CONFIRM_VALUES}


def clean(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """The hand-edited section → ``(values, warning codes)``; never raises.

    A section that is not an object is empty (``section_invalid``). A sub-cap above the
    section's own cap is left as is here: :func:`.allowance.resolve_values` rounds it
    against the **effective** cap (which may come from a variable).
    """
    if raw is None:
        return {}, []
    if type(raw) is not dict:
        return {}, ["section_invalid"]
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in raw.items():
        if key not in FIELDS:
            warnings.append("unknown_key")
            continue
        if key in _CHOICES:
            if type(value) is str and value in _CHOICES[key]:
                values[key] = value
            elif key == "allow":
                values[key] = "off"  # not understood: the safe side
                warnings.append("allow_invalid")
            else:
                warnings.append(f"{key}_invalid")
        elif key == "offer_exec":
            if type(value) is bool:
                values[key] = value
            else:
                values[key] = False  # not understood: not offered
                warnings.append("offer_exec_invalid")
        elif type(value) is int:
            rounded = min(max(value, LOW[key]), CAP_MAX)
            values[key] = rounded
            if rounded != value:
                warnings.append(f"{key}_rounded")
        else:
            warnings.append(f"{key}_invalid")
    return values, sorted(set(warnings))


def validate(values: Mapping[str, Any], cap: int | None = None) -> list[dict[str, Any]]:
    """Field errors of a save (``[]`` = valid). ``cap``: the effective cap the sub-caps are
    checked against (a variable's value when the cap is pinned); default: the section's
    own ``max_auto_approvals``, else the default cap."""
    errors: list[dict[str, Any]] = []
    for key, value in values.items():
        if key not in FIELDS:
            errors.append({"field": str(key)[:64], "code": "unknown_field", "params": {}})
        elif key in _CHOICES:
            if type(value) is not str or value not in _CHOICES[key]:
                errors.append(
                    {"field": key, "code": "invalid_choice", "params": {"choices": _CHOICES[key]}}
                )
        elif key == "offer_exec":
            if type(value) is not bool:
                errors.append({"field": key, "code": "not_boolean", "params": {}})
        elif type(value) is not int or not LOW[key] <= value <= CAP_MAX:
            errors.append(
                {
                    "field": key,
                    "code": "out_of_range",
                    "params": {"min": LOW[key], "max": CAP_MAX},
                }
            )
    if errors:
        return errors
    if cap is None:
        cap = values.get("max_auto_approvals", DEFAULT_CAP)
    for key in ("max_external", "max_exec"):
        if key in values and values[key] > cap:
            errors.append({"field": key, "code": "exceeds_cap", "params": {"max": cap}})
    return errors
