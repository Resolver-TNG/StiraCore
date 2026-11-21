"""The context window a local OpenAI-compatible server reports (#746).

llama.cpp-class servers answer ``GET <base_url>/models`` with ``meta.n_ctx`` for the
loaded model. When no window is configured or recorded for the model, that number is
used instead of the 200,000 fallback (which put the summary threshold above a 131k
window in the measured trial).

Bounds, all fixed:

* only the providers and addresses of the #745 local rule (``openai`` /
  ``openai_responses`` with a loopback or RFC 1918 ``base_url``, decided from the URL
  text, no DNS lookup); every other spec is answered ``None`` without a request;
* one ``GET`` to the endpoint the model calls already go to: no credentials are sent,
  no proxy is used, redirects are not followed, the body is capped, the wait is short;
* the answer (also "nothing reported") is cached per endpoint and model id;
* only a positive integer leaves this module. Nothing is logged: no URL, no body.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .capabilities import _LOCAL_ENDPOINT_PROVIDERS, is_local_endpoint
from .spec import ModelSpec

FETCH_TIMEOUT_SEC = 2.0
CACHE_TTL_SEC = 300.0
MAX_BODY_BYTES = 1024 * 1024
# A larger value is not a window any server has: treated as not reported.
MAX_REPORTED_WINDOW = 100_000_000

_cache: dict[tuple[str, str], tuple[float, int | None]] = {}
_lock = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _http_get_json(url: str, timeout: float) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES])


def _models_url(base_url: Any) -> str | None:
    if not is_local_endpoint(base_url):
        return None
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return None
    return base_url.strip().rstrip("/") + "/models"


def window_from_models(payload: Any, model_id: str) -> int | None:
    """``meta.n_ctx`` of ``model_id`` in a ``/models`` answer, or of the only model listed."""
    entries = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        return None
    entries = [entry for entry in entries if isinstance(entry, Mapping)]
    named = [entry for entry in entries if entry.get("id") == model_id]
    chosen = named[0] if named else entries[0] if len(entries) == 1 else None
    meta = chosen.get("meta") if chosen is not None else None
    value = meta.get("n_ctx") if isinstance(meta, Mapping) else None
    if type(value) is int and 0 < value <= MAX_REPORTED_WINDOW:
        return value
    return None


def reported_context_window(
    spec: Any,
    *,
    cached_only: bool = False,
    fetch: Callable[[str, float], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """The window the local server of ``spec`` reports, or ``None`` (not local, not reported).

    ``cached_only`` never sends a request (status views). ``fetch(url, timeout)`` returns
    the decoded JSON body and ``clock`` is the cache's time source (tests pass both).
    """
    if not isinstance(spec, ModelSpec) or spec.provider not in _LOCAL_ENDPOINT_PROVIDERS:
        return None
    url = _models_url(spec.options.get("base_url"))
    if url is None:
        return None
    key = (url, spec.model_id)
    now = clock()
    with _lock:
        cached = _cache.get(key)
    if cached is not None and (cached_only or now < cached[0]):
        return cached[1]
    if cached_only:
        return None
    try:
        window = window_from_models(
            (fetch or _http_get_json)(url, FETCH_TIMEOUT_SEC), spec.model_id
        )
    except Exception:  # noqa: BLE001 - unreachable or not JSON: the window stays unknown
        window = None
    with _lock:
        _cache[key] = (now + CACHE_TTL_SEC, window)
    return window


def clear_cache() -> None:
    with _lock:
        _cache.clear()


__all__ = ["clear_cache", "reported_context_window", "window_from_models"]
