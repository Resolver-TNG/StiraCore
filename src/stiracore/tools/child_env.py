"""The environment an exec-class child process gets (#457): an allowlist, never the server's.

The chat server's process holds the provider keys and tokens it uses itself
(``AWS_*``, ``*_API_KEY``, ``GITHUB_TOKEN`` …). A command an approved ``shell`` call
starts must not inherit them: a model could print them (``env``) and a later call send
them elsewhere. The child gets only the names below that it needs to run (search path,
home, locale, terminal, temporary directory), and even those are dropped when their name
looks like a key/token/secret or their value is one ``mask_secrets`` would change.

An allowlist, not a denylist: a new variable of the server process is never passed
unless it is added here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

CHILD_ENV_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TZ",
        "COLUMNS",
        "LINES",
        "NO_COLOR",
        # Locale: listed one by one (an ``LC_`` prefix would also pass ``LC_API_TOKEN``).
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "LC_MONETARY",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Windows (unverified): needed to start a process.
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)
# Names that are never passed, even when listed (belt and braces for the list above).
SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE|PRIVATE|CERT)",
    re.IGNORECASE,
)


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted subset of ``environ`` (default: ``os.environ``) for a child process."""
    from stiracore.observability.masking import mask_secrets

    source = os.environ if environ is None else environ
    kept = {}
    for name in CHILD_ENV_ALLOWED:
        value = source.get(name)
        if not isinstance(value, str) or SECRET_NAME.search(name):
            continue
        if mask_secrets(value) != value:
            continue
        kept[name] = value
    return kept


__all__ = ["CHILD_ENV_ALLOWED", "SECRET_NAME", "child_environment"]
