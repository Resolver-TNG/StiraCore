"""Per-app navigation and status wiring for the shared App shell (#183).

stira_server and the observer dashboard (dashboard/app.py) serve different page sets,
so each app declares its own sidebar items; base.html never links to a page the app
does not serve. Apps that do not call install_shell() get DEFAULT_NAV (stage-1 shell).
"""

from __future__ import annotations

from collections.abc import Iterable

# key -> (href, catalog label key)
NAV_ITEMS: dict[str, tuple[str, str]] = {
    "observer": ("/", "base.observer"),
    "chat": ("/chat", "base.chat"),
    "approvals": ("/approvals", "base.approvals"),
    "schedules": ("/schedules", "base.schedules"),
    "memory": ("/memory", "base.memory"),
    "artifacts": ("/artifacts", "base.artifacts"),  # #454
    "observability": ("/observability", "base.observability"),
    "settings": ("/settings", "base.settings"),
    # Opt-in (STIRA_STAGE=1, #341): the chat server adds it to its nav only when enabled.
    "stage": ("/stage/", "base.stage"),
}
# Tool permissions live in the settings hub (#227); /tools redirects there.
DEFAULT_NAV = ("chat", "settings", "observability")
STIRA_NAV = (
    "chat",
    "approvals",
    "schedules",
    "memory",
    "artifacts",
    "observability",
    "settings",
)


def observer_nav(has_chat: bool) -> tuple[str, ...]:
    """Observer dashboard pages; /chat only exists when a chat agent is attached."""
    chat = ("chat",) if has_chat else ()
    return ("observer", *chat, "observability", "settings")


def nav_entries(keys: Iterable[str]) -> list[tuple[str, str, str]]:
    entries = []
    for key in keys:
        if key not in NAV_ITEMS:
            raise ValueError(f"unknown nav item: {key}")
        href, label = NAV_ITEMS[key]
        entries.append((key, href, label))
    return entries


def install_shell(
    app,
    nav: Iterable[str],
    *,
    status_url: str = "/api/gateway/status",
    approvals_url: str | None = "/api/hitl/pending",
    approvals_page: str | None = None,
) -> list[tuple[str, str, str]]:
    """Expose `shell_nav` and `shell_status` to templates (rendered by base.html).

    status_url must return JSON with a breaker level (`breaker` / `circuit_breaker`);
    approvals_url=None hides the pending-approvals chip (the app has no HITL broker).
    approvals_page is where the chip links (default: /approvals when it is in the nav,
    otherwise /chat), so the chip, the chat cards and the page show the same queue (#189).
    """
    entries = nav_entries(nav)
    if approvals_page is None:
        approvals_page = "/approvals" if any(key == "approvals" for key, *_ in entries) else "/chat"
    status = {
        "status_url": status_url,
        "approvals_url": approvals_url or "",
        "approvals_page": approvals_page,
    }
    app.extensions["stira_shell"] = {"nav": entries, **status}

    @app.context_processor
    def _shell_context():
        return {"shell_nav": entries, "shell_status": status}

    return entries
