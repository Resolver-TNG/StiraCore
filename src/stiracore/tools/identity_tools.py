"""Execution-boundary wrappers for the standard file mutation tools."""

import copy
import dataclasses
import json
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from typing import Any

from strands.tools.tools import AgentTool

# The SDK executor only treats a ToolResultEvent (or a bare ToolResult as the
# last raw value) as the result; a plain {"tool_result": ...} dict became a
# malformed toolResult in real agent loops (#145).
from strands.types._events import ToolResultEvent

from stiracore.identity_paths import PathDecision, WritePathPolicy

# Tools whose ``path`` argument is the write target (write roots apply to it).
PATH_WRITE_TOOLS = frozenset({"file_write", "editor"})

from .file_editor_tool import EDITOR_TOOL_NAME, from_editor_path, to_editor_path
from .file_tools import (
    DIAGNOSIS_MARKER,
    OBSERVATION_MARKER,
    _cwd_absolute,
    _error,
    floor_refusal,
    marked_block,
    observe_written,
    read_floor,
    shown_path,
)
from .tool_consent import (
    GateMark,
    ToolConsentError,
    bridge_refusal,
    claim_gate,
    consent_granted,
    held_mark,
    input_digest,
)


def _refused(tool_use: dict, text: str) -> ToolResultEvent:
    return ToolResultEvent(
        {"toolUseId": tool_use.get("toolUseId"), "status": "error", "content": [{"text": text}]}
    )


def _with_shown_paths(event: Any, raw_paths: Iterable[Any]) -> Any:
    """``file_write``'s own result text with every raw target path shown (#307).

    The upstream tool echoes the path it wrote (``File written successfully to
    <path>``, ``Error writing file ... '<path>'``); here it passes ``shown_path``
    like every Stira diagnosis and observation (secret-looking → ``[masked]``,
    home → ``~``).
    """
    if not isinstance(event, ToolResultEvent) or not isinstance(event.tool_result, Mapping):
        return event
    pairs = []
    for raw in raw_paths:
        if isinstance(raw, str) and raw.strip():
            shown = shown_path(raw)
            if shown is not None and shown != raw:
                pairs.append((raw, shown))
    if not pairs:
        return event
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    result = event.tool_result
    blocks = []
    for block in result.get("content") or ():
        text = block.get("text") if isinstance(block, Mapping) else None
        if isinstance(text, str):
            for raw, shown in pairs:
                text = text.replace(raw, shown)
            block = {**block, "text": text}
        blocks.append(block)
    return ToolResultEvent({**result, "content": blocks}, event.exception)


def _with_observation(event: Any, path: str, content: Any, opened: list) -> Any:
    """A successful ``file_write`` result plus the observation of what it wrote (#307)."""
    if not isinstance(event, ToolResultEvent):
        return event
    result = event.tool_result
    if not isinstance(result, Mapping) or result.get("status") != "success":
        return event
    blocks = list(result.get("content") or [])
    if any(
        isinstance(block, Mapping) and str(block.get("text", "")).startswith(OBSERVATION_MARKER)
        for block in blocks
    ):
        return event  # an inner wrapper (nested policies) already measured it
    blocks.append(marked_block(OBSERVATION_MARKER, observe_written(path, content, opened)))
    return ToolResultEvent({**result, "content": blocks}, event.exception)


def editor_read_only(tool_name: str, arguments: dict) -> bool:
    return tool_name == "editor" and arguments.get("command") in {"view", "find_line"}


def check_tool_write(
    policy: WritePathPolicy, tool_name: str, arguments: Any
) -> PathDecision | None:
    """The shared write-target decision (#243) for #4 (HITL), #5 (IdentityGuard), #6.

    ``None`` for tools without a structured write path (they only get the
    argument floor scan of IdentityGuard). Directories ``makedirs`` would create
    are checked by the policy. ``editor`` writes no ``.bak`` any more (#245).
    """
    if tool_name not in PATH_WRITE_TOOLS:
        return None
    if not isinstance(arguments, Mapping):
        return PathDecision(False, "invalid", "Tool arguments must be an object.")
    if editor_read_only(tool_name, arguments):
        return None
    path = arguments.get("path")
    if tool_name == EDITOR_TOOL_NAME and isinstance(path, str):
        path = from_editor_path(path)  # Windows: /C:/x as the editor echoes it
    return policy.check(path)


class IdentityProtectedTool(AgentTool):
    """Keep the upstream schema/implementation, enforcing policy before execution.

    This wrapper also protects direct SDK tool invocations without NXP hooks.
    It is not a sandbox for shell, batch, or arbitrary third-party tool code.
    """

    def __init__(
        self,
        wrapped: AgentTool,
        read_only_paths: Iterable[str] = (),
        protected_dirs: Iterable[str] = (),
        *,
        policy: WritePathPolicy | None = None,
    ) -> None:
        super().__init__()
        self.wrapped = wrapped
        read_only_paths, protected_dirs = tuple(read_only_paths), tuple(protected_dirs)
        if policy is None:
            self.policy = WritePathPolicy(read_only_paths, protected_dirs=protected_dirs)
        elif not read_only_paths and not protected_dirs:
            self.policy = policy
        else:
            self.policy = policy.with_floor(read_only_paths, protected_dirs)

    @property
    def tool_name(self):
        return self.wrapped.tool_name

    @property
    def tool_spec(self):
        return self.wrapped.tool_spec

    @property
    def tool_type(self):
        return self.wrapped.tool_type

    def _editor_target(self, arguments: Any, checked: str | None) -> str | None:
        """The path to hand Stira's editor: the checked one, or (``view``) anchored too."""
        if checked:
            return to_editor_path(checked)
        path = arguments.get("path") if isinstance(arguments, Mapping) else None
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return None
        return to_editor_path(str(self.policy.absolute(from_editor_path(path))))

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        arguments = tool_use["input"]
        requested_path = arguments.get("path") if isinstance(arguments, Mapping) else None
        # #242: a mutating call runs from a snapshot of its arguments, consumes the
        # gate's single-use mark for exactly those arguments and needs a verified
        # bridge; the consent grant is held only while one upstream step runs.
        mutating = isinstance(arguments, Mapping) and not editor_read_only(
            self.tool_name, arguments
        )
        mark: GateMark | str | None = None
        if mutating:
            try:
                arguments = copy.deepcopy(arguments)
            except Exception:  # noqa: BLE001 - uncopyable input cannot be approved
                yield _refused(tool_use, f"{self.tool_name} was not run: input cannot be copied.")
                return
        decision = check_tool_write(self.policy, self.tool_name, arguments)
        if decision is not None and not decision.allowed:
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "error",
                    "content": [{"text": decision.reason}],
                }
            )
            return
        if self.tool_name == EDITOR_TOOL_NAME and not mutating:
            # ``view``: the read floor (#312) before the sandbox reads or lists anything.
            viewed = self._editor_target(arguments, None)
            refusal = (
                floor_refusal(self.policy, from_editor_path(viewed), self.tool_name)
                if viewed
                else None
            )
            if refusal is not None:
                yield ToolResultEvent(
                    {
                        "toolUseId": tool_use.get("toolUseId"),
                        **_error(f"editor refused: {refusal} ({refusal.reason})", refusal),
                    }
                )
                return
        if mutating:
            held = held_mark(self.tool_name)
            if (
                held is not None
                and held.tool_use_id == tool_use.get("toolUseId")
                and held.digest == input_digest(arguments)
            ):
                mark = held  # nested wrapper inside a step already claimed
            else:
                mark = claim_gate(
                    self.tool_name, invocation_state, tool_use.get("toolUseId"), arguments
                )
            refusal = mark if isinstance(mark, str) else bridge_refusal(self.tool_name)
            if refusal:
                yield _refused(tool_use, refusal)
                return
        checked = decision.path if decision is not None else None
        target = checked
        if self.tool_name == EDITOR_TOOL_NAME:
            target = self._editor_target(arguments, checked)
        if target and target != arguments.get("path"):
            # Write exactly what was checked: relative paths anchor to the
            # workspace, never to whatever the process cwd happens to be.
            arguments = {**arguments, "path": target}
        if mutating:
            tool_use = {**tool_use, "input": arguments}
            # A nested wrapper sees the arguments as passed down (resolved path).
            mark = dataclasses.replace(mark, digest=input_digest(arguments))
        elif arguments is not tool_use["input"]:
            tool_use = {**tool_use, "input": arguments}
        events = self.wrapped.stream(tool_use, invocation_state, **kwargs)
        opened: list = []  # files the upstream step opened for writing (#307)
        try:
            while True:
                try:
                    grant = (
                        consent_granted(
                            self.tool_name,
                            mark,
                            write_path=checked,
                            policy=self.policy,
                            opened=opened,
                        )
                        if mutating
                        else nullcontext()
                    )
                    # The editor's sandbox reads and lists under the read floor (#312).
                    floor = (
                        read_floor(self.policy, self.tool_name)
                        if self.tool_name == EDITOR_TOOL_NAME
                        else nullcontext()
                    )
                    with grant, floor:
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                except ToolConsentError as error:
                    yield _refused(tool_use, str(error))
                    return
                if mutating and self.tool_name == "file_write":
                    event = _with_shown_paths(event, (checked, requested_path))
                if mutating and checked and self.tool_name == "file_write":
                    # After the grant is released: computed from the requested bytes,
                    # checked against the inode the step opened (#307).
                    event = _with_observation(event, checked, arguments.get("content"), opened)
                yield event
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()


class _AnchorError(ValueError):
    """The workspace base of a relative read path could not be resolved."""


class WorkspaceAnchoredReadTool(AgentTool):
    """Anchor a read tool's relative path to the workspace, behind the read floor.

    #306: ``file_write`` resolves a relative ``path`` against the workspace; the bare
    read tools resolve it against the process cwd, so a file just written could not be
    read back by the same name. This wrapper rewrites only that one argument to the
    absolute path the write side would use (``anchor=False`` keeps the tool's own cwd
    base, for ``lsp_*``). Name, spec and type are the wrapped tool's (the tool list
    stays cache-stable, #254).

    #312: the path is checked against the read floor (``WritePathPolicy.check_read``,
    the data-directory floor of #271) before the tool runs, and each step of the tool
    runs with the policy as the active read floor, so the tool's own opens are checked
    again before and after opening (``file_tools.open_for_read``). A refusal is
    ``floor``; no approval lifts it.
    """

    def __init__(
        self,
        wrapped: AgentTool,
        policy: WritePathPolicy,
        argument: str = "path",
        *,
        anchor: bool = True,
    ) -> None:
        super().__init__()
        self.wrapped = wrapped
        self.policy = policy
        self.argument = argument
        self.anchor = anchor

    @property
    def tool_name(self):
        return self.wrapped.tool_name

    @property
    def tool_spec(self):
        return self.wrapped.tool_spec

    @property
    def tool_type(self):
        return self.wrapped.tool_type

    def _anchored(self, arguments: Any) -> Any:
        if not isinstance(arguments, Mapping):
            return arguments
        path = arguments.get(self.argument)
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return arguments
        if not self.anchor:
            return arguments
        try:
            absolute = str(self.policy.absolute(path))
        except (OSError, RuntimeError, ValueError) as error:
            # Fail closed: never fall back to the process cwd (the base this wrapper exists to
            # replace) when the workspace base cannot be resolved.
            raise _AnchorError(
                f"relative path could not be resolved ({type(error).__name__})"
            ) from error
        return {**arguments, self.argument: absolute}

    def _annotated(self, event: ToolResultEvent, requested: str, anchored: Any) -> ToolResultEvent:
        """A failed read's diagnosis block gains what was asked and the base used (#307)."""
        result = event.tool_result
        if not isinstance(result, Mapping) or result.get("status") != "error":
            return event
        changed, content = False, []
        for block in result.get("content") or ():
            text = block.get("text") if isinstance(block, Mapping) else None
            if isinstance(text, str) and text.startswith(DIAGNOSIS_MARKER + " "):
                try:
                    data = json.loads(text[len(DIAGNOSIS_MARKER) + 1 :])
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    data["requested_path"] = shown_path(requested)
                    if anchored != requested:
                        data["base_dir"] = shown_path(str(self.policy.base_dir))
                    block, changed = marked_block(DIAGNOSIS_MARKER, data), True
            content.append(block)
        if not changed:
            return event
        return ToolResultEvent({**result, "content": content}, event.exception)

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        try:
            anchored = self._anchored(tool_use.get("input"))
        except _AnchorError as error:
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use.get("toolUseId"),
                    "status": "error",
                    "content": [{"text": f"Refused: {error}"}],
                }
            )
            return
        requested = tool_use.get("input")
        requested = requested.get(self.argument) if isinstance(requested, Mapping) else None
        target = anchored.get(self.argument) if isinstance(anchored, Mapping) else None
        if isinstance(target, str) and target.strip() and "\0" not in target:
            refusal = floor_refusal(self.policy, _cwd_absolute(target), self.tool_name)
            if refusal is not None:
                event = ToolResultEvent(
                    {
                        "toolUseId": tool_use.get("toolUseId"),
                        **_error(
                            f"{self.tool_name} refused: {refusal} ({refusal.reason})", refusal
                        ),
                    }
                )
                yield self._annotated(event, requested, target)
                return
        tool_use = {**tool_use, "input": anchored}
        events = self.wrapped.stream(tool_use, invocation_state, **kwargs)
        try:
            while True:
                try:
                    # The read floor is active only while one step of the tool runs.
                    with read_floor(self.policy, self.tool_name):
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                if isinstance(event, ToolResultEvent) and isinstance(requested, str):
                    event = self._annotated(event, requested, target)
                yield event
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
