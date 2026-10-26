"""The settings surface manifest (#468) and the per-field facts the settings hub shows (#469).

``settings_surface.json`` (next to this file) classifies every setting: the
``GatewayConfig`` / ``AgentConfig`` / ``StiraConfig`` fields and every documented
environment variable. Per item: the class (A on the page or planned, B confirmed, C not on
the page with a reason, D needs a decision), whether the page can change it, when a saved
value takes effect and which variable wins. ``docs/settings-surface.md`` is rendered from it
(:func:`render_markdown`); ``tests/test_settings_surface.py`` fails on an unclassified field
or variable.

The hub uses :func:`describe` for ``source`` (env / agent / gateway / default) and
``applies`` per field, :func:`pinned_variables` for the variables that pin a setting, and
:func:`saved_snapshot` + :func:`restart_pending` for "saved, waits for a restart".
Only variable **names** leave this module: an environment value is read to decide whether
the variable is set, never returned.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("settings_surface.json")
KINDS = ("env", "dev", "gateway", "agent", "stira")
CONFIG_KINDS = ("gateway", "agent", "stira")
SOURCES = ("env", "agent", "gateway", "default")
# How a variable counts as set: ``present`` (also when empty; apply_env_overrides) or
# ``non_empty`` (``raw.strip()`` must be non-empty; the boolean switches).
WHEN = ("present", "non_empty")
_ITEM_KEYS = {
    "id",
    "class",
    "ui",
    "applies",
    "reason",
    "overrides",
    "when",
    "group",
    "issue",
    "note",
    "agent_field",
    # per_field items only: the keys of the object that apply at a restart (gateway.ui:
    # ``home`` is read at start-up, ``board_svg`` on each Stage load).
    "restart_keys",
}


class ManifestError(ValueError):
    """The manifest file does not follow its own schema."""


def problems(data: Any) -> list[str]:
    """Schema problems of a manifest (empty = valid). C needs a known reason."""
    found: list[str] = []
    if not isinstance(data, Mapping):
        return ["manifest is not an object"]
    vocab = {key: data.get(key) for key in ("classes", "ui", "applies", "reasons")}
    for key, value in vocab.items():
        if not isinstance(value, Mapping) or not value:
            found.append(f"{key}: missing")
    if found:
        return found
    items = data.get("items")
    if not isinstance(items, list):
        return ["items: not a list"]
    seen: set[str] = set()
    ids = {item.get("id") for item in items if isinstance(item, Mapping)}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            found.append(f"items[{index}]: not an object")
            continue
        ident = item.get("id")
        label = ident if isinstance(ident, str) else f"items[{index}]"
        if not isinstance(ident, str) or ident.partition(".")[0] not in KINDS:
            found.append(f"{label}: id must be <kind>.<name>, kind in {KINDS}")
            continue
        if ident in seen:
            found.append(f"{label}: duplicate")
        seen.add(ident)
        unknown = sorted(set(item) - _ITEM_KEYS)
        if unknown:
            found.append(f"{label}: unknown keys {unknown}")
        for key in ("class", "ui", "applies"):
            if item.get(key) not in vocab[{"class": "classes"}.get(key, key)]:
                found.append(f"{label}: {key} {item.get(key)!r} is not in the vocabulary")
        reason = item.get("reason")
        if item.get("class") == "C" and not reason:
            found.append(f"{label}: class C needs a reason")
        if reason is not None and reason not in vocab["reasons"]:
            found.append(f"{label}: reason {reason!r} is not in the vocabulary")
        if item.get("when") is not None and item.get("when") not in WHEN:
            found.append(f"{label}: when must be one of {WHEN}")
        for target in item.get("overrides") or ():
            if target not in ids or target.partition(".")[0] not in CONFIG_KINDS:
                found.append(f"{label}: overrides {target!r}, not a config item")
        restart_keys = item.get("restart_keys")
        if restart_keys is not None and (
            item.get("applies") != "per_field"
            or not isinstance(restart_keys, list)
            or not restart_keys
            or not all(isinstance(key, str) and key for key in restart_keys)
        ):
            found.append(f"{label}: restart_keys must be a list of names on a per_field item")
        if item.get("overrides") and not ident.startswith("env."):
            found.append(f"{label}: only env items override")
    return found


@lru_cache(maxsize=1)
def _cached() -> dict:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    found = problems(data)
    if found:
        raise ManifestError("; ".join(found))
    return data


def load_manifest() -> dict:
    """The validated manifest (a copy; the cached one is never handed out)."""
    return copy.deepcopy(_cached())


def items(kind: str | None = None) -> list[dict]:
    rows = _cached()["items"]
    return [
        copy.deepcopy(row) for row in rows if kind is None or row["id"].partition(".")[0] == kind
    ]


def _name(item_id: str) -> str:
    return item_id.partition(".")[2]


def variable_set(name: str, env_values: Mapping[str, str], when: str = "non_empty") -> bool:
    """Whether the variable takes effect. Reads the value only to test it; never returns it."""
    if name not in env_values:
        return False
    if when == "present":
        return True
    raw = env_values.get(name)
    return isinstance(raw, str) and bool(raw.strip())


# ADR 0012 (phase 1): with the YOLO allowance set, the existing read APIs stay byte-identical
# (tests/test_yolo_default_unchanged.py). The YOLO section shows its own variables
# (``GET /api/yolo/settings`` → ``pinned``), so the hub leaves them out.
HUB_SILENT_REASONS = frozenset({"adr_0012"})


def _hub_reports(item: Mapping) -> bool:
    return item.get("reason") not in HUB_SILENT_REASONS


def _env_by_target() -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for item in _cached()["items"]:
        if not _hub_reports(item):
            continue
        for target in item.get("overrides") or ():
            found.setdefault(target, []).append(item)
    return found


def _plain(value: Any) -> Any:
    """A JSON-comparable copy (dataclasses such as ToolConfig compare by ``as_dict``)."""
    if hasattr(value, "as_dict") and callable(value.as_dict):
        value = value.as_dict()
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        value = sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _defaults() -> tuple[Any, Any]:
    from stiracore.config.models import AgentConfig, GatewayConfig

    return GatewayConfig(), AgentConfig()


def _saved_value(item_id: str, gateway, agent) -> Any:
    kind, name = item_id.partition(".")[0], _name(item_id)
    if kind == "gateway":
        return getattr(gateway, name, None)
    if kind == "agent":
        return getattr(agent, name, None)
    if name == "identity_anchors":
        return list(agent.identity_anchors)
    return (agent.stira_config_overrides or {}).get(name)


def _source(item: Mapping, gateway, agent, defaults, pinned: bool, agent_wins: bool) -> str:
    if agent_wins:
        return "agent"
    if pinned:
        return "env"
    kind, name = item["id"].partition(".")[0], _name(item["id"])
    default_gateway, default_agent = defaults
    if kind == "gateway":
        same = _plain(getattr(gateway, name, None)) == _plain(getattr(default_gateway, name, None))
        return "default" if same else "gateway"
    if kind == "agent":
        same = _plain(getattr(agent, name, None)) == _plain(getattr(default_agent, name, None))
        return "default" if same else "agent"
    if name == "identity_anchors":
        same = _plain(agent.identity_anchors) == _plain(default_agent.identity_anchors)
        return "default" if same else "agent"
    return "agent" if name in (agent.stira_config_overrides or {}) else "default"


def _set_by(variables: list[dict], env_values: Mapping[str, str]) -> str | None:
    return next(
        (
            _name(var["id"])
            for var in variables
            if variable_set(_name(var["id"]), env_values, var.get("when", "non_empty"))
        ),
        None,
    )


def _agent_wins(item: Mapping, agent, by_target, env_values: Mapping[str, str]) -> bool:
    """A gateway field whose ``agent_field`` is set takes the agent value, unless a variable
    that overrides the agent field itself is set (resolver: ``STIRA_PROVIDER`` drops the
    whole agent provider bundle, ``STIRA_MODEL`` the model override). The agent value then
    wins over the field's own variable too: ``provider_override`` brings ``base_url`` and
    ``api_key_env`` with it, even when they are None, and ``STIRA_BASE_URL`` is not used."""
    agent_field = item.get("agent_field")
    if not agent_field or getattr(agent, agent_field, None) is None:
        return False
    return _set_by(by_target.get(f"agent.{agent_field}", []), env_values) is None


def pinned_by(item_id: str, env_values: Mapping[str, str]) -> str | None:
    """The name of the variable that pins one config field (the rule of :func:`describe`,
    without the agent bundle), or ``None``. A section's server check uses it (#470)."""
    return _set_by(_env_by_target().get(item_id, []), env_values)


def describe(gateway, agent, env_values: Mapping[str, str]) -> dict[str, dict]:
    """Per config field: class, ui, applies, group, the variables that win, which one is
    set (``pinned_by``, a name) and the ``source`` of the value in effect."""
    by_target = _env_by_target()
    defaults = _defaults()
    result = {}
    for item in _cached()["items"]:
        if item["id"].partition(".")[0] not in CONFIG_KINDS:
            continue
        variables = by_target.get(item["id"], [])
        agent_wins = _agent_wins(item, agent, by_target, env_values)
        pinned_by = None if agent_wins else _set_by(variables, env_values)
        result[item["id"]] = {
            "class": item["class"],
            "ui": item["ui"],
            "applies": item["applies"],
            "group": item.get("group"),
            "issue": item.get("issue"),
            "env": [_name(var["id"]) for var in variables],
            "pinned_by": pinned_by,
            "source": _source(item, gateway, agent, defaults, pinned_by is not None, agent_wins),
        }
    return result


def pinned_variables(env_values: Mapping[str, str]) -> list[dict]:
    """Variables that are set and pin a setting (A/B items or ones that override a field):
    ``[{"name", "fields", "applies"}]``. Names only."""
    found = []
    for item in _cached()["items"]:
        if not item["id"].startswith("env.") or not _hub_reports(item):
            continue
        if not (item.get("overrides") or item["class"] in ("A", "B")):
            continue
        name = _name(item["id"])
        if variable_set(name, env_values, item.get("when", "non_empty")):
            found.append(
                {
                    "name": name,
                    "fields": list(item.get("overrides") or ()),
                    "applies": item["applies"],
                    "class": item["class"],
                    "issue": item.get("issue"),
                }
            )
    return found


def _restart_value(item: Mapping, gateway, agent) -> Any:
    value = _plain(_saved_value(item["id"], gateway, agent))
    keys = item.get("restart_keys")
    if keys is None:
        return value
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) for key in keys}


def saved_snapshot(gateway, agent) -> dict[str, Any]:
    """The saved values of every field that applies at a restart (taken at start-up); for a
    ``per_field`` item with ``restart_keys``, only those keys."""
    return {
        item["id"]: _restart_value(item, gateway, agent)
        for item in _cached()["items"]
        if item["id"].partition(".")[0] in CONFIG_KINDS
        and (item["applies"] == "restart" or item.get("restart_keys"))
    }


def restart_pending(
    start: Mapping[str, Any] | None, gateway, agent, fields: Mapping[str, dict]
) -> list[str]:
    """Restart fields saved since the start whose value a variable does not pin."""
    if start is None:
        return []
    now = saved_snapshot(gateway, agent)
    return [
        item_id
        for item_id, value in now.items()
        if item_id in start
        and start[item_id] != value
        and not (fields.get(item_id) or {}).get("pinned_by")
    ]


def pending_groups(pending: list[str], fields: Mapping[str, dict]) -> list[str]:
    """One entry per change the person made: fields of one group count once."""
    seen: list[str] = []
    for item_id in pending:
        key = (fields.get(item_id) or {}).get("group") or item_id
        if key not in seen:
            seen.append(key)
    return seen


# --- docs/settings-surface.md ------------------------------------------------------------

_TITLES = {
    "env": "Environment variables (server)",
    "dev": "Environment variables (scripts and tests)",
    "gateway": "`gateway-config.json` (`GatewayConfig`)",
    "agent": "`agent-config.json` (`AgentConfig`)",
    "stira": "`stira_config_overrides` (`StiraConfig`)",
}


def render_markdown(data: Mapping | None = None) -> str:
    """The generated block of ``docs/settings-surface.md`` (between its markers)."""
    data = data or _cached()
    by_target: dict[str, list[str]] = {}
    for item in data["items"]:
        for target in item.get("overrides") or ():
            by_target.setdefault(target, []).append(_name(item["id"]))
    lines: list[str] = []
    for kind in KINDS:
        rows = [item for item in data["items"] if item["id"].partition(".")[0] == kind]
        lines += [f"### {_TITLES[kind]}", ""]
        if kind in CONFIG_KINDS:
            lines.append("| Setting | Class | UI | Applies | Variable that wins | Reason / note |")
        else:
            lines.append("| Setting | Class | UI | Applies | Overrides | Reason / note |")
        lines.append("|---|---|---|---|---|---|")
        for item in rows:
            if kind in CONFIG_KINDS:
                linked = by_target.get(item["id"], [])
            else:
                linked = list(item.get("overrides") or ())
            notes = [
                f"`{item['reason']}`" if item.get("reason") else "",
                item.get("issue") or "",
                item.get("note") or "",
            ]
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_name(item['id'])}`",
                        item["class"],
                        item["ui"],
                        item["applies"],
                        ", ".join(f"`{name}`" for name in linked) or "—",
                        " ".join(part for part in notes if part) or "—",
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "CONFIG_KINDS",
    "KINDS",
    "MANIFEST_PATH",
    "SOURCES",
    "ManifestError",
    "describe",
    "items",
    "load_manifest",
    "pending_groups",
    "pinned_by",
    "pinned_variables",
    "problems",
    "render_markdown",
    "restart_pending",
    "saved_snapshot",
    "variable_set",
]


if __name__ == "__main__":  # pragma: no cover - prints the docs block
    print(render_markdown(), end="")
