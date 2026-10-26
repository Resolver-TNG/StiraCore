"""Keep the server log readable: no access-log line for a successful page poll (#369).

The pages poll a handful of read-only endpoints every few seconds (turns, pending
approvals, gateway status, ...). Their 200/304 lines are most of ``stira.out.log`` and
bury WARNING/ERROR lines. :class:`PollingAccessFilter` drops exactly those lines from
werkzeug's access log; a failed poll (any other status), every other path and every method
other than GET are still logged. ``STIRA_ACCESS_LOG_POLLING=1`` logs them again (debugging).
"""

from __future__ import annotations

import logging
import os
import re

ENV = "STIRA_ACCESS_LOG_POLLING"

# Read-only GETs the pages poll: route -> the exact query string the polling code sends
# (a regex; "" = none). The same route with another query is a different read (an inspector
# opening a turn, a history restore) and stays in the log.
POLLED: dict[str, re.Pattern[str]] = {
    "/api/turns": re.compile(r"limit=1"),
    "/api/hitl/pending": re.compile(r""),
    "/api/hitl/history": re.compile(r"limit=50"),
    "/api/gateway/status": re.compile(r""),
    "/api/diagnostics/write-failures": re.compile(r""),
    "/api/scheduler": re.compile(r""),
    "/api/state": re.compile(r""),
    "/api/history": re.compile(r""),
    "/api/state/history": re.compile(r"limit=500"),
}
# The Stage follows a turn with after_seq=<last seen seq>; after_seq=0 is the first read
# of a turn (also what the inspector sends) and is logged.
_TURN_EVENTS = re.compile(r"^/api/turns/[^/]+/events$")
_TURN_EVENTS_QUERY = re.compile(r"after_seq=[1-9]\d*")
_REQUEST = re.compile(r"^GET (\S+) HTTP/[\d.]+$")
_QUIET_STATUS = frozenset({"200", "304"})


def is_polled(path: str) -> bool:
    route, _, query = path.partition("?")
    if _TURN_EVENTS.match(route):
        return bool(_TURN_EVENTS_QUERY.fullmatch(query))
    pattern = POLLED.get(route)
    return pattern is not None and pattern.fullmatch(query) is not None


class PollingAccessFilter(logging.Filter):
    """Drop werkzeug's successful access-log lines of the polled endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        if os.environ.get(ENV) == "1":
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) != 3 or str(args[1]) not in _QUIET_STATUS:
            return True
        match = _REQUEST.match(str(args[0]))
        return not (match and is_polled(match.group(1)))


def install_polling_filter() -> PollingAccessFilter:
    """Add the filter to werkzeug's logger once (idempotent); return the installed one."""
    logger = logging.getLogger("werkzeug")
    for existing in logger.filters:
        if isinstance(existing, PollingAccessFilter):
            return existing
    added = PollingAccessFilter()
    logger.addFilter(added)
    return added
