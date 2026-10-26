"""Memory autonomy (ADR 0018, phase 0 — observe only): settings, turn trust, in-turn ratings.

**Off unless configured.** ``agent-config.json`` → ``memory_autonomy`` (absent or ``{}`` = off:
no ``memory_rate`` tool, no memory job kind, nothing bound). ``STIRA_MEMORY_AUTONOMY=0``
forces it off whatever the file says. Phase 0 is the only phase this code accepts; phases 1
and 2 each need the commander's go (ADR 0018, Decisions).

```json
"memory_autonomy": {
  "phase": 0,
  "in_turn_rating": true,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": false},
  "daily_token_budget": 100000,
  "undo_window_days": 30
}
```

**In-turn rating** (:class:`RatingSession`, the harness side of ``memory_rate``). The model
supplies ``importance``, ``reason`` and optionally ``half_life_days``; the harness stamps the
rest. The rating waits for the turn's episode and is bound to that episode's exact text
(``memory_rating.bind_rating``) after ``record_episode`` wrote it. One rating per turn;
at most :data:`~stiracore.memory_rating.HIGH_DAILY_QUOTA` ratings ``>= 0.8`` per UTC
day (further ones are stored as 0.79, ``capped = quota``); refused outside a NORMAL breaker;
``invariant`` is never accepted. **The stored rating is used by nothing in phase 0** — not
by Harvest, recall, forgetting or retention.

**Turn trust** (:func:`turn_trust`, ADR 0018 §1e, commander decision Q8): a turn is
``trusted`` only when a person's message started it and the conversation in the context
holds no result of a tool outside :data:`TRUSTED_RESULT_TOOLS` (web, file, LSP, MCP,
child-agent, memory read-back, anything unknown — fail closed). Turn-level and conservative
until #458 adds data-level labels.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from stiracore import memory_rating as rating_store

__all__ = [
    "DAILY_TOKEN_BUDGET_MAX",
    "ENV_VAR",
    "TRUSTED_RESULT_TOOLS",
    "MemoryAutonomySettings",
    "RatingSession",
    "clean_options",
    "settings_from",
    "target_ref",
    "turn_trust",
]

logger = logging.getLogger(__name__)

ENV_VAR = "STIRA_MEMORY_AUTONOMY"
PHASES = (0,)
JOB_KINDS = ("memory_rate_backfill", "memory_consolidate", "memory_tidy")
DAILY_TOKEN_BUDGET_MAX = 100_000  # commander decision Q5: total across all memory jobs
UNDO_WINDOW_DEFAULT = 30
_KEYS = frozenset({"phase", "in_turn_rating", "jobs", "daily_token_budget", "undo_window_days"})
_FALSY = frozenset({"0", "false", "no", "off"})

# Tools whose result is produced by the harness itself, not by outside or stored content.
# Everything else that ran in the context makes the turn ``untrusted`` (ADR 0018 Q8): web,
# file and LSP reads, MCP tools, child agents (``delegate``, ``use_agent``, ...), memory
# read-back (episodes carry no trust label before #458) and any name not listed here.
TRUSTED_RESULT_TOOLS = frozenset(
    {
        "current_time",
        "stop",
        "handoff_to_user",
        "memory_rate",
        "memory_write",
        "handoff_write",
        "memory_list",
        "journal",
        "propose_procedure",
    }
)


@dataclass(frozen=True)
class MemoryAutonomySettings:
    enabled: bool = False
    phase: int = 0
    in_turn_rating: bool = False
    jobs: frozenset[str] = frozenset()
    daily_token_budget: int = DAILY_TOKEN_BUDGET_MAX
    undo_window_days: int = UNDO_WINDOW_DEFAULT
    forced_off: bool = False

    @property
    def rating_enabled(self) -> bool:
        return self.enabled and self.in_turn_rating

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "phase": self.phase if self.enabled else None,
            "forced_off": self.forced_off,
            "in_turn_rating": self.rating_enabled,
            "job_kinds": sorted(self.jobs) if self.enabled else [],
            "daily_token_budget": self.daily_token_budget if self.enabled else None,
        }


def _int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"memory_autonomy.{name} must be an integer from {low} to {high}")
    return value


def clean_options(value: Any) -> dict[str, Any]:
    """Validate the ``memory_autonomy`` block (``AgentConfig``); ``{}`` = off."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("memory_autonomy must be an object")  # noqa: TRY004 - config contract
    if not value:
        return {}
    unknown = sorted(set(value) - _KEYS)
    if unknown:
        raise ValueError(f"memory_autonomy: unknown key(s) {', '.join(unknown)}")
    if "phase" not in value:
        raise ValueError("memory_autonomy.phase is required (0: observe only)")
    phase = value["phase"]
    if type(phase) is not int or phase not in PHASES:
        raise ValueError(
            "memory_autonomy.phase must be 0 (phases 1 and 2 are not authorised yet, ADR 0018)"
        )
    clean: dict[str, Any] = {"phase": phase}
    rating = value.get("in_turn_rating", True)
    if type(rating) is not bool:
        raise ValueError("memory_autonomy.in_turn_rating must be a boolean")
    clean["in_turn_rating"] = rating
    jobs = value.get("jobs", {})
    if not isinstance(jobs, dict):
        raise ValueError("memory_autonomy.jobs must be an object")  # noqa: TRY004 - config contract
    unknown = sorted(set(jobs) - set(JOB_KINDS))
    if unknown:
        raise ValueError(f"memory_autonomy.jobs: unknown kind(s) {', '.join(unknown)}")
    if any(type(flag) is not bool for flag in jobs.values()):
        raise ValueError("memory_autonomy.jobs values must be booleans")
    clean["jobs"] = {kind: bool(jobs.get(kind, False)) for kind in JOB_KINDS}
    clean["daily_token_budget"] = _int(
        value.get("daily_token_budget", DAILY_TOKEN_BUDGET_MAX),
        "daily_token_budget",
        0,
        DAILY_TOKEN_BUDGET_MAX,
    )
    clean["undo_window_days"] = _int(
        value.get("undo_window_days", UNDO_WINDOW_DEFAULT), "undo_window_days", 1, 365
    )
    return clean


def settings_from(options: Any, environ: Mapping[str, str] | None = None) -> MemoryAutonomySettings:
    """The effective settings. Never raises: an invalid block counts as off (logged)."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR)
    if raw is not None and raw.strip().lower() in _FALSY:
        return MemoryAutonomySettings(forced_off=True)
    try:
        clean = clean_options(options)
    except ValueError as error:
        logger.warning("memory_autonomy off: invalid settings (%s)", type(error).__name__)
        return MemoryAutonomySettings()
    if not clean:
        return MemoryAutonomySettings()
    return MemoryAutonomySettings(
        enabled=True,
        phase=clean["phase"],
        in_turn_rating=clean["in_turn_rating"],
        jobs=frozenset(kind for kind, on in clean["jobs"].items() if on),
        daily_token_budget=clean["daily_token_budget"],
        undo_window_days=clean["undo_window_days"],
    )


# -- turn trust ---------------------------------------------------------------------------


def _tool_names(messages: Any) -> Iterable[Any]:
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("toolUse"), Mapping):
                yield block["toolUse"].get("name")


def turn_trust(messages: Any, *, started_by_person: bool) -> str:
    """``trusted`` / ``untrusted`` for the turn whose context is ``messages`` (fail closed)."""
    if not started_by_person:
        return "untrusted"  # a webhook, a bot, an automatic continue: not a person's words
    if messages is None:
        messages = []
    if not isinstance(messages, (list, tuple)):
        return "untrusted"
    try:
        names = list(_tool_names(messages))
    except Exception:  # noqa: BLE001 - an unreadable context is untrusted
        return "untrusted"
    for name in names:
        if type(name) is not str or name not in TRUSTED_RESULT_TOOLS:
            return "untrusted"
    return "trusted"


# -- audit references ------------------------------------------------------------------------

_TARGET_KEY = secrets.token_bytes(32)  # keyed hashes of this process only (as YOLO's)


def target_ref(layer: Any, key: str) -> str:
    """The keyed hash of an entry's key for an audit row (keys can carry a memory's words)."""
    name = f"{getattr(layer, 'value', layer)}/{key}".encode()
    return hmac.new(_TARGET_KEY, name, hashlib.sha256).hexdigest()


def _round05(value: float) -> float:
    return round(round(value * 20) / 20, 2)


# -- the harness side of memory_rate -------------------------------------------------------------


@dataclass
class _Pending:
    turn_id: str
    rating: dict[str, Any]
    by_person: bool
    model_ref: str | None
    extra_capped: tuple[str, ...] = ()


@dataclass
class RatingSession:
    """One per chat server. ``rate`` is the tool's handler; ``bind_turn`` runs after the turn's
    episode is written; ``discard`` drops a pending rating whose turn wrote no episode."""

    store: Any
    turn_id: Callable[[], str | None]
    breaker_level: Callable[[], Any]
    started_by_person: Callable[[], bool] = lambda: False
    model_ref: Callable[[], str | None] = lambda: None
    audit: Callable[..., Any] | None = None
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _pending: dict[str, _Pending] = field(default_factory=dict, init=False)
    _rated_turns: set[str] = field(default_factory=set, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _level(self) -> str:
        try:
            level = self.breaker_level()
        except Exception:  # noqa: BLE001 - unknown is not NORMAL
            return "unknown"
        name = getattr(level, "value", getattr(level, "name", level))
        return str(name).rsplit(".", 1)[-1].strip().lower()

    def _audit(self, outcome: str, **fields: Any) -> None:
        if self.audit is None:
            return
        try:
            self.audit(outcome, **fields)
        except Exception as error:  # noqa: BLE001 - evidence only; never fails the turn
            logger.warning("memory audit row not written (%s)", type(error).__name__)

    def rate(self, importance: Any, reason: Any, half_life_days: Any = None) -> dict[str, Any]:
        """Store a pending rating for the running turn. Raises ``RatingRefused``."""
        turn_id = self.turn_id()
        try:
            if not isinstance(turn_id, str) or not turn_id:
                raise rating_store.RatingRefused("E_NO_TURN")
            if self._level() != "normal":
                raise rating_store.RatingRefused("E_BREAKER_NOT_NORMAL")
            cleaned = rating_store.clean_rating(importance, reason, half_life_days)
            with self._lock:
                if turn_id in self._rated_turns:
                    raise rating_store.RatingRefused("E_ONE_PER_TURN")
                extra: tuple[str, ...] = ()
                if cleaned["importance"] >= rating_store.HIGH_RATING:
                    day = self.now_fn().astimezone(UTC).strftime("%Y-%m-%d")
                    used = rating_store.high_ratings_on(self.store, day)
                    if used >= rating_store.HIGH_DAILY_QUOTA:
                        cleaned = {**cleaned, "importance": rating_store.QUOTA_VALUE}
                        extra = ("quota",)
                if len(self._rated_turns) >= 256:  # turns that ended without an episode
                    self._rated_turns.clear()
                self._rated_turns.add(turn_id)
                # Only the running turn may hold a pending rating (bounded memory).
                self._pending = {
                    turn_id: _Pending(
                        turn_id,
                        cleaned,
                        bool(self.started_by_person()),
                        self._model_ref(),
                        extra,
                    )
                }
        except rating_store.RatingRefused as error:
            self._audit("refused", turn_id=turn_id, rated_by="agent_turn", reason_code=error.code)
            raise
        return {**cleaned, "capped": [*cleaned["capped"], *extra]}

    def _model_ref(self) -> str | None:
        try:
            value = self.model_ref()
        except Exception:  # noqa: BLE001 - provenance is best effort
            return None
        return value[:120] if isinstance(value, str) and value else None

    def discard(self, turn_id: str | None) -> None:
        with self._lock:
            if turn_id is not None:
                self._pending.pop(turn_id, None)
                self._rated_turns.discard(turn_id)

    def bind_turn(self, turn_id: str | None, entry: Any, messages: Any) -> dict | None:
        """Bind the turn's pending rating to the episode ``entry`` just written. Never raises."""
        with self._lock:
            pending = self._pending.pop(turn_id, None) if turn_id is not None else None
            self._rated_turns.discard(turn_id)  # the turn is over
        if pending is None or entry is None:
            return None
        try:
            from stiracore.tools.memory_store import MemoryLayer

            layer = MemoryLayer.EPISODIC
            text = self.store.read_text(layer, entry.key)
            if text is None or self.store._parse(text)[0] != entry.content:
                raise rating_store.RatingRefused("E_EPISODE_CHANGED")
            trust = turn_trust(messages, started_by_person=pending.by_person)
            extra = pending.extra_capped + (("untrusted",) if trust != "trusted" else ())
            row = rating_store.bind_rating(
                self.store,
                layer,
                entry.key,
                text,
                rating=pending.rating,
                rated_by="agent_turn",
                trust=trust,
                turn_id=pending.turn_id,
                model_ref=pending.model_ref,
                rule_importance=rating_store.harness_rule_importance(
                    self.store, layer, entry.key, text
                ),
                extra_capped=extra,
                at=self.now_fn(),
            )
        except rating_store.RatingRefused as error:
            self._audit("refused", turn_id=turn_id, rated_by="agent_turn", reason_code=error.code)
            return None
        except Exception as error:  # noqa: BLE001 - the turn already succeeded
            logger.warning("memory rating not bound (E_RATING_NOT_BOUND, %s)", type(error).__name__)
            self._audit(
                "refused", turn_id=turn_id, rated_by="agent_turn", reason_code="E_RATING_NOT_BOUND"
            )
            return None
        self._audit(
            "rated",
            layer=row["layer"],
            target=target_ref(layer, entry.key),
            turn_id=pending.turn_id,
            rated_by="agent_turn",
            trust=row["trust"],
            capped=row["capped"],
            rating=_round05(row["agent_importance"]),
            reason_code=row["agent_reason"],
        )
        return row
