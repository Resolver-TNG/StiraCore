"""Tool-call checks for protected identity files and write blocking."""

from __future__ import annotations

import ast
import fnmatch
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from stiracore.core.engine import HITL_APPROVED_TOOL_IDS_KEY, BreakAction, StiraEngine
from stiracore.identity_paths import READ_ONLY_ERROR, WritePathPolicy, refusal_detail
from stiracore.tools.identity_tools import check_tool_write, editor_read_only
from stiracore.tools.os_protection import is_command_tool, os_protection_denial
from stiracore.tools.tool_registry import is_exec_tool


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str
    layer: int | None = None


_WRITE_TOOLS = frozenset(
    {
        "write",
        "write_file",
        "file_write",
        "edit",
        "edit_file",
        "file_edit",
        "editor",
        "str_replace",
        "str_replace_editor",
        "apply_patch",
        "patch_file",
        "create_file",
        "delete_file",
        "remove_file",
        "rename_file",
        "move_file",
        "copy_file",
        "append_file",
        "shell",
        "bash",
        "terminal",
        "execute_command",
        "run_command",
    }
)
_WRITE_VERBS = frozenset(
    {"write", "edit", "patch", "create", "delete", "remove", "rename", "move", "append"}
)
_PYTHON_COMMANDS = frozenset({"python", "python3", "python.exe", "python3.exe"})


def _strings(value: Any, seen: set[int] | None = None) -> Iterator[str]:
    """Inspect nested arguments without recursing forever on cyclic input."""
    if isinstance(value, str):
        yield value
        return
    if not isinstance(value, (Mapping, list, tuple)):
        return
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key, seen)
            yield from _strings(item, seen)
    else:
        for item in value:
            yield from _strings(item, seen)


# Arguments that carry the *body* of a write, never its target. Their text is prose, code
# or data and may contain bare ``/``, ``~``, ``.``, ``..`` or ``/Users`` tokens, which the
# path scan would read as the ancestor of a read-only file (#190 adds absolute
# data-directory files to the floor). One declaration per tool: the argument names that are
# body text for *that* tool (#304, #464). A tool that is not declared here, or a key that is
# not declared for it, is still scanned in full (fail closed). Each declared tool decides
# its target from the other arguments, which stay scanned:
# - file_write / editor: ``path`` (and ``check_tool_write`` / ``WritePathPolicy``)
# - memory_write: ``layer`` / ``key`` (``MemoryStore`` keeps the file under the memory dir
#   and the tool writes the episodic layer only)
# - journal: ``date`` (must be absent or YYYY-MM-DD, see ``_journal_date_denial``)
# - web_search: no target at all; ``query`` is only sent as ``?q=`` to the configured
#   search endpoint (``tools/web_tools.py``), never opened or written locally (#464)
# Measured and kept scanned (#464 survey, ``tests/test_identity_guard_body_survey.py``):
# ``speak.text`` (``say`` reads a leading ``-o<path>`` as its output file), the prompts of
# the tools that start another agent (``think`` / ``use_agent`` / ``swarm`` / ``graph`` /
# ``delegate``: that agent may act on the text) and ``journal.task``.
_FILE_BODY_KEYS = frozenset({"content", "file_text", "new_str", "old_str"})
_BODY_ARGUMENTS: Mapping[str, frozenset[str]] = {
    "file_write": _FILE_BODY_KEYS,
    "editor": _FILE_BODY_KEYS,
    "memory_write": frozenset({"content"}),
    # handoff_write: the store keeps it under episodic/handoff/; ``pointers`` stay scanned
    "handoff_write": frozenset({"goal", "state", "decisions", "open_questions"}),
    "journal": frozenset({"content"}),
    # propose_procedure (#463): ``key`` (one validated component; the store keeps the file
    # under procedural/staging/)
    "propose_procedure": frozenset({"body", "rationale"}),
    "web_search": frozenset({"query"}),
}
# Argument names that usually hold body text: a floor refusal on one of them, for a tool
# that does not declare it as body, says so (requirement 4 of #464).
_BODY_LIKE_KEYS = _FILE_BODY_KEYS | {"text", "body", "message", "task", "note"}
# ``strands_tools.journal`` writes ``<cwd>/journal/<date>.md`` with ``date`` joined as given.
_JOURNAL_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _named_strings(
    value: Any, name: str, seen: set[int] | None = None
) -> Iterator[tuple[str, str]]:
    """``(argument name, string)`` for the same strings, in the same order, as ``_strings``.

    The name is the argument's key path (``edits[0].path``); a key is reported under
    its own path. Used only to say *which* argument a refusal reacted to (#307).
    """
    if isinstance(value, str):
        yield name, value
        return
    if not isinstance(value, (Mapping, list, tuple)):
        return
    if seen is None:
        seen = set()
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{name}.{key}" if name else str(key)
            yield from _named_strings(key, child, seen)
            yield from _named_strings(item, child, seen)
    else:
        for index, item in enumerate(value):
            yield from _named_strings(item, f"{name}[{index}]", seen)


def _scanned_named(tool_name: str, tool_args: Any) -> Iterator[tuple[str, str]]:
    """``(argument name, string)`` of ``tool_args`` that may name a write target.

    A command-capable tool (shell, batch, ...) names its targets anywhere in its text, so
    nothing is skipped for it. For a tool in ``_BODY_ARGUMENTS`` its declared body keys are
    left out; every other tool is scanned in full.
    """
    body_keys = _BODY_ARGUMENTS.get(tool_name)
    if (
        not isinstance(tool_args, Mapping)
        or not body_keys
        or is_command_tool(tool_name)
        or is_exec_tool(tool_name)
    ):
        yield from _named_strings(tool_args, "" if isinstance(tool_args, Mapping) else "input")
        return
    for key, value in tool_args.items():
        yield from _named_strings(key, str(key))
        # Only a plain string under a declared body key is prose; a nested structure under
        # that key could carry a target, so it is still scanned.
        if key in body_keys and isinstance(value, str):
            continue
        yield from _named_strings(value, str(key))


def _scanned_strings(tool_name: str, tool_args: Any) -> Iterator[str]:
    """The strings of ``tool_args`` that may name a write target (see ``_scanned_named``)."""
    for _name, text in _scanned_named(tool_name, tool_args):
        yield text


def _journal_date_denial(tool_name: str, tool_args: Any) -> str | None:
    """Why a ``journal`` call's ``date`` is refused, or ``None``.

    The body of ``journal`` is not scanned (#464), so its target must be decided by
    ``date`` alone: absent / ``None`` (today) or an exact ``YYYY-MM-DD``. Anything else
    (``../SOUL``, ``/``, ``2026/10/19``) would be joined into the path as given.
    """
    if tool_name != "journal" or not isinstance(tool_args, Mapping):
        return None
    date = tool_args.get("date")
    if date is None or (type(date) is str and _JOURNAL_DATE.fullmatch(date)):
        return None
    return (
        'journal "date" must be a day written as YYYY-MM-DD (e.g. 2026-10-19), '
        "or left out for today."
    )


def _speak_option_denial(tool_name: str, tool_args: Any) -> str | None:
    """Why a ``speak`` call's ``text`` is refused, or ``None`` (#464 survey).

    ``strands_tools.speak`` runs ``subprocess.run(["say", text])``: macOS ``say`` reads a
    ``text`` that starts with ``-`` as options (``-o<path>`` writes an audio file there,
    measured), so such a text is a write target outside the floor scan. Only a leading
    ``-`` is an option (``say`` stops at the first word that is not one).
    """
    if tool_name != "speak" or not isinstance(tool_args, Mapping):
        return None
    text = tool_args.get("text")
    if isinstance(text, str) and text.startswith("-"):
        return 'speak "text" must not start with "-" (say would read it as options).'
    return None


def _undeclared_body_note(tool_name: str, argument: str) -> str:
    """A hint on a floor refusal that reacted to a body-like argument (#464)."""
    if argument not in _BODY_LIKE_KEYS or argument in _BODY_ARGUMENTS.get(tool_name, ()):
        return ""
    return (
        f' ("{argument}" was read as a possible write target: it is not declared as body '
        f"text for this tool)"
    )


def _python_path_candidates(text: str) -> Iterator[str]:
    """Expose literal paths embedded in Python inline commands.

    Shell tools are already classified as write-capable. Conservatively
    inspect all string literals in inline Python rather than trying to prove
    which calls write: aliases and pathlib methods can also mutate files.

    Parsing never executes code. This handles literal paths, not arbitrary
    computed paths, and is not a substitute for an execution sandbox.
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return

    for index, token in enumerate(tokens):
        executable = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if executable not in _PYTHON_COMMANDS:
            continue

        # Permit interpreter options before -c, but do not cross a shell
        # command boundary looking for an inline program.
        for argument_index in range(index + 1, len(tokens)):
            argument = tokens[argument_index]
            if argument and all(char in ";&|()" for char in argument):
                break
            if argument != "-c":
                continue
            if argument_index + 1 >= len(tokens):
                break

            source = tokens[argument_index + 1]
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError, RecursionError):
                break

            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    yield node.value.replace("\\", "/").strip()
            break


def _path_candidates(text: str) -> Iterator[str]:
    normalized = text.replace("\\", "/").strip()
    yield normalized
    # Also catch paths in command arguments and multiline patches.
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = normalized.split()
    for token in tokens:
        yield token
        if "=" in token:
            yield token.split("=", 1)[1]

    # Parse the original text: normalizing backslashes before parsing could
    # change Python string escapes or shell quoting.
    yield from _python_path_candidates(text)


class IdentityGuard(HookProvider):
    """Inspect tool arguments, never assistant prose.

    Glob matching is lexical; exact identity paths also resolve filesystem aliases.
    Arbitrary shell-program analysis still requires an execution sandbox.
    Configured blocked tools are treated as write-capable tools.
    """

    def __init__(
        self,
        engine: StiraEngine,
        protected_patterns: list[str] | None = None,
        *,
        read_only_paths: Iterable[str] = (),
        path_policy: WritePathPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.protected_patterns = tuple(p.replace("\\", "/") for p in (protected_patterns or ()))
        # One policy object shared with the file-tool wrapper and the HITL gate
        # (#243): floor (identity, read-only paths, protected_dirs) + write roots.
        if path_policy is None:
            self.path_policy = WritePathPolicy(read_only_paths)
        else:
            self.path_policy = path_policy.with_floor(read_only_paths)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool:
            return
        approved = event.invocation_state.get(HITL_APPROVED_TOOL_IDS_KEY, set())
        result = self.check_tool_call(
            event.tool_use["name"],
            event.tool_use["input"],
            hitl_approved=str(event.tool_use.get("toolUseId", "")) in approved,
        )
        if not result.allowed:
            event.cancel_tool = result.reason

    def _configured_tools(self) -> set[str]:
        config = getattr(self.engine, "config", None)
        if isinstance(config, Mapping):
            names = config.get("blocked_tool_names", ())
        else:
            names = getattr(config, "blocked_tool_names", ())
        return set(names or ())

    def _protected_match(self, tool_name: str, tool_args: dict) -> tuple[str, str] | None:
        """The first ``(argument name, token)`` naming a protected path, or ``None``.

        The decision is the same as before #307 (any match denies). For the report,
        a match of the whole string (e.g. a shell command ending in ``> SOUL.md``)
        is narrowed to the first matching token inside it when there is one.
        """
        for name, text in _scanned_named(tool_name, tool_args):
            whole = None
            for candidate in _path_candidates(text):
                if not self._candidate_protected(candidate):
                    continue
                if whole is None and candidate == text.replace("\\", "/").strip():
                    whole = candidate
                    continue
                return name, candidate
            if whole is not None:
                return name, whole
        return None

    def _candidate_protected(self, candidate: str) -> bool:
        try:
            if self.path_policy.is_read_only(candidate):
                return True
        except (OSError, ValueError, RuntimeError):
            # Prose may not be a valid OS path; retain lexical checks.
            pass
        for pattern in self.protected_patterns:
            if fnmatch.fnmatchcase(candidate, pattern):
                return True
            # */soul.md should protect a root-relative "soul.md" too.
            if pattern.startswith("*/") and fnmatch.fnmatchcase(
                "/" + candidate.lstrip("/"), pattern
            ):
                return True
        return False

    def _is_protected(self, tool_name: str, tool_args: dict) -> bool:
        return self._protected_match(tool_name, tool_args) is not None

    @staticmethod
    def _registered_write(tool_name: str) -> bool:
        """Names in the live write registry, including MCP tools added later (#145)."""
        from stiracore.tools.tool_registry import WRITE_TOOLS

        return tool_name in WRITE_TOOLS

    @staticmethod
    def _is_write_tool(tool_name: str) -> bool:
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool_name).lower()
        parts = re.split(r"[^a-z0-9]+", name)
        return (
            name in _WRITE_TOOLS
            or bool(_WRITE_VERBS.intersection(parts))
            or any(part in {"shell", "bash", "terminal"} for part in parts)
        )

    def _requires_confirmation(self) -> bool:
        status = getattr(self.engine, "breaker_status", None)
        actions = getattr(status(), "actions", None) if callable(status) else None
        return bool(actions is not None and actions & BreakAction.REQUIRE_CONFIRMATION)

    def _write_capable(self, tool_name: str, tool_args: dict) -> bool:
        if editor_read_only(tool_name, tool_args):
            return False
        blocked_names = self._configured_tools() | set(self.engine.blocked_tools())
        return (
            self._is_write_tool(tool_name)
            or tool_name in blocked_names
            or self._registered_write(tool_name)
        )

    def path_denial(self, tool_name: str, tool_args: dict) -> GuardResult | None:
        """The path part of the decision, shared with the HITL gate (#4, #243).

        Denials here hold at every breaker level and no approval lifts them:
        the floor for any write-capable tool's arguments, plus the write roots
        for the structured ``path`` of ``file_write``/``editor``. Command-capable
        tools whose input names xattr/chflags/sudo/tccutil/csrutil in plain text
        are refused here too, at any level (#244; computed names: #167).
        """
        os_problem = os_protection_denial(tool_name, tool_args)
        if os_problem is not None:
            return GuardResult(False, os_problem, 3)
        if not self._write_capable(tool_name, tool_args):
            return None
        speak_problem = _speak_option_denial(tool_name, tool_args)
        if speak_problem is not None:
            return GuardResult(False, speak_problem + refusal_detail("input", "text", "-"))
        # journal's body is not scanned (#464): its target is ``date`` alone. Checked
        # first so the refusal says how to write the day (any bad date is refused).
        journal_problem = _journal_date_denial(tool_name, tool_args)
        if journal_problem is not None:
            date = tool_args.get("date")
            return GuardResult(False, journal_problem + refusal_detail("input", "date", date))
        # Identity paths are protected at every circuit-breaker level.
        match = self._protected_match(tool_name, tool_args)
        if match is not None:
            # Which argument and token it reacted to (#304/#307): body text is no target.
            reason = READ_ONLY_ERROR + refusal_detail("floor", *match)
            return GuardResult(False, reason + _undeclared_body_note(tool_name, match[0]), 3)
        decision = check_tool_write(self.path_policy, tool_name, tool_args)
        if decision is not None and not decision.allowed:
            layer = "floor" if decision.code == "floor" else decision.code
            path = tool_args.get("path") if isinstance(tool_args, Mapping) else None
            return GuardResult(
                False,
                decision.reason + refusal_detail(layer, "path", path),
                3 if decision.code == "floor" else None,
            )
        return None

    def check_tool_call(
        self, tool_name: str, tool_args: dict, *, hitl_approved: bool = False
    ) -> GuardResult:
        write_capable = self._write_capable(tool_name, tool_args)
        denial = self.path_denial(tool_name, tool_args)
        if denial is not None:
            return denial

        if write_capable and self.engine.is_write_blocked() and not hitl_approved:
            return GuardResult(
                False,
                "Writes are blocked by the circuit breaker.",
                None,
            )

        # WARNING has no confirmation gateway yet, so NXPAgent drops the named
        # write tools; also deny write-shaped names that were not registered (#145).
        if write_capable and self._requires_confirmation() and not hitl_approved:
            return GuardResult(
                False,
                "Write tools require confirmation at WARNING; no confirmation gateway exists.",
                None,
            )

        return GuardResult(True, "Tool call allowed.")
