"""Stira's URL tools never reach the dashboard they run under (#495).

``http_request`` (and ``rss`` when it is installed) take a URL from the model. With a
loosened rule (``http_request`` Always) the agent could otherwise call the dashboard's own
settings / approval API: ``/api/csrf`` hands the token to any local client and a request
without ``Origin`` passes (those checks stop browsers, not a same-host client).

Three checks, each enough on its own for the case it covers, all independent of YOLO and
of the tool rule (a person's approval of the call does not lift it; there is no setting):

1. :class:`OwnDashboardGuardedTool` — before the upstream tool runs, the URL is parsed and
   its host **resolved** (``own_dashboard.url_refusal``); an answer that is the dashboard
   (loopback / unspecified / this host's addresses at the dashboard's port) refuses the
   call with ``E_OWN_DASHBOARD``. Nothing is sent.
2. :func:`install_redirect_check` — every redirect hop of ``http_request`` is checked the
   same way before it is followed.
3. The connect hook of ``own_dashboard`` (armed by the server at every start) — the address
   actually connected to, for a second DNS answer (rebinding) or a URL the client reads
   differently than the check did.

Other ports on loopback (a local model or embeddings server) and every external URL are not
touched. Out of scope (accepted residual): other processes of the same OS user (MCP
servers, exec children) and anything that is not in the server process.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

from stiracore.yolo import own_dashboard

_LOG = logging.getLogger(__name__)

REASON_CODE = own_dashboard.REASON_CODE
# The tools whose input names a URL to connect to, and that argument.
URL_ARGUMENTS = {"http_request": "url", "rss": "url"}


def _refused(tool_use: Mapping, name: str, reason: str) -> ToolResultEvent:
    text = (
        f"{name} was not run: {reason}. The URL reaches this Stira dashboard (its settings "
        "and approvals); Stira's tools never call their own dashboard, also when the call "
        "was approved. Ask the person to make the change on the dashboard."
    )
    return ToolResultEvent(
        {"toolUseId": tool_use.get("toolUseId"), "status": "error", "content": [{"text": text}]}
    )


class OwnDashboardGuardedTool(AgentTool):
    """Keep the upstream tool's name/spec/type; refuse a URL that is the own dashboard."""

    def __init__(self, wrapped: AgentTool) -> None:
        super().__init__()
        if wrapped.tool_name not in URL_ARGUMENTS:
            raise ValueError(f"{wrapped.tool_name} has no URL argument known to Stira")
        self.wrapped = wrapped

    @property
    def tool_name(self):
        return self.wrapped.tool_name

    @property
    def tool_spec(self):
        return self.wrapped.tool_spec

    @property
    def tool_type(self):
        return self.wrapped.tool_type

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        arguments = tool_use.get("input")
        url = (
            arguments.get(URL_ARGUMENTS[self.tool_name]) if isinstance(arguments, Mapping) else None
        )
        reason = own_dashboard.url_refusal(url) if url is not None else None
        if reason is not None:
            yield _refused(tool_use, self.tool_name, reason)
            return
        async for event in self.wrapped.stream(tool_use, invocation_state, **kwargs):
            yield event


def install_redirect_check(module) -> bool:
    """Check every redirect hop of the upstream ``http_request`` before it is followed.

    The upstream tool builds its sessions from ``module._SafeRedirectSession``; Stira's
    subclass checks the next hop's URL in ``rebuild_auth`` (called by ``requests`` for each
    redirect before it is sent) and raises :class:`own_dashboard.OwnDashboardRefused`,
    which the upstream tool reports as an error result. Returns False (logged) when the
    upstream shape is not there; the connect hook still holds then.
    """
    base = getattr(module, "_SafeRedirectSession", None)
    if base is None or not hasattr(base, "rebuild_auth"):
        _LOG.warning("http_request: redirect check not installed (upstream shape changed)")
        return False
    if getattr(base, "_stira_own_dashboard_check", False):
        return True

    class _OwnDashboardRedirectSession(base):
        _stira_own_dashboard_check = True

        def rebuild_auth(self, prepared_request, response):
            if own_dashboard.url_refusal(prepared_request.url) is not None:
                raise own_dashboard.OwnDashboardRefused()
            return super().rebuild_auth(prepared_request, response)

    module._SafeRedirectSession = _OwnDashboardRedirectSession
    cache = getattr(module, "SESSION_CACHE", None)
    if isinstance(cache, dict):
        cache.clear()  # no session built from the unchecked class stays in use
    return True


_MISSING_LOGGED = False


def install_registered_redirect_check(tools: Mapping, modules: Mapping) -> bool:
    """Install :func:`install_redirect_check` when ``http_request`` is registered (#499).

    The upstream module is found in ``modules`` (``sys.modules``): the registry imports it
    when it registers the tool. When the tool is registered but its module is not loaded
    (a lazy import), the hop check cannot be installed: that is logged once — the URL check
    before sending and the connect hook still hold.
    """
    global _MISSING_LOGGED
    if "http_request" not in tools:
        return False
    module = modules.get("strands_tools.http_request")
    if module is None:
        if not _MISSING_LOGGED:
            _MISSING_LOGGED = True
            _LOG.warning(
                "http_request: redirect check not installed (strands_tools.http_request is "
                "not loaded); the URL check and the connect hook still refuse the dashboard"
            )
        return False
    return install_redirect_check(module)


__all__ = [
    "REASON_CODE",
    "URL_ARGUMENTS",
    "OwnDashboardGuardedTool",
    "install_redirect_check",
    "install_registered_redirect_check",
]
