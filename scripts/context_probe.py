"""Measure context management (#258): tokens per turn with large tool results, ON vs OFF.

Offline (default, no network): a scripted model replays the same tool calls an
assistant would make; every request is sized with the Strands SDK's token
estimate (tiktoken when installed, else chars/4), tools and system included.

    PYTHONPATH=<worktree>/src:<worktree> python scripts/context_probe.py

Live (Bedrock, real answers; the AWS credential chain is used, nothing else):

    PYTHONPATH=... python scripts/context_probe.py --live --max-calls 20

Each condition runs the same three large-result tasks (file read, search, HTTP
fetch) whose answer sits outside the preview, then asks about an earlier result
again. ``--max-calls`` is a hard cap on model calls across both conditions; the
run stops at the cap. Results are printed as Markdown tables.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from strands import tool
from strands.models import Model

from stiracore.agent.context_management import RETRIEVAL_TOOL_NAME, ContextManagementSettings
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.stimulus_classifier import RuleBasedClassifier

INPUT_PRICE = 2.0  # $ per million input tokens (Sonnet 5.5 list price, no cache)
OUTPUT_PRICE = 10.0  # $ per million output tokens
SYSTEM = "You are a careful assistant. Use tools to answer. Answer briefly with the exact value."

# --------------------------------------------------------------------- synthetic data


def report_text() -> str:
    lines = [f"line {n}: value-{n * 7}" for n in range(1, 2001)]
    lines[1311] = "line 1312: config max_retries = 17"
    return "\n".join(lines)


def search_results() -> list[dict[str, Any]]:
    hits = [
        {
            "rank": n,
            "title": f"Release notes {n // 10}.{n % 10}",
            "snippet": "Routine maintenance release with dependency updates. " * 4,
            "url": f"https://example.test/releases/{n}",
        }
        for n in range(1, 81)
    ]
    hits[56] = {
        "rank": 57,
        "title": "Release 0.9.3",
        "snippet": "Release 0.9.3 was published on 2026-09-02 and fixes the scheduler.",
        "url": "https://example.test/releases/57",
    }
    return hits


def page_text() -> str:
    filler = "<p>Thank you for shopping with us. Your items are being prepared.</p>\n"
    return filler * 220 + "<p>Order number: QX-48213</p>\n" + filler * 220


@tool
def file_read(path: str) -> str:
    """Read a text file and return its content."""
    return report_text()


@tool
def web_search(query: str) -> dict:
    """Search the web; returns ranked hits as JSON."""
    return {"status": "success", "content": [{"json": {"query": query, "hits": search_results()}}]}


@tool
def http_get(url: str) -> str:
    """Fetch a URL and return the response body."""
    return page_text()


TOOLS = (file_read, web_search, http_get)

TASKS = (
    ("Read report.txt and tell me the max_retries value.", "17"),
    ("Search for 'release 0.9.3' and tell me its publication date.", "2026-09-02"),
    ("Fetch https://example.test/order and tell me the order number.", "QX-48213"),
    ("What is on line 1500 of report.txt?", "10500"),
)

# --------------------------------------------------------------------- offline


class ScriptedModel(Model):
    """Replays tool calls per task and records the SDK token estimate of every request."""

    def __init__(self, managed: bool) -> None:
        self.config: dict[str, Any] = {"model_id": "scripted"}
        self.managed = managed
        self.calls: list[dict[str, Any]] = []
        self.plan: list[list[dict[str, Any]]] = []

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    async def structured_output(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError
        yield

    def queue(self, task_index: int) -> None:
        tool_calls = [
            [("file_read", {"path": "report.txt"})],
            [("web_search", {"query": "release 0.9.3"})],
            [("http_get", {"url": "https://example.test/order"})],
            [],
        ][task_index]
        retrieve = {
            0: {"pattern": "max_retries"},
            1: {"pattern": "0\\.9\\.3"},
            2: {"pattern": "Order number"},
            3: {"pattern": "^line 1500:"},
        }[task_index]
        steps: list[list[dict[str, Any]]] = [
            [{"tool": name, "input": args}] for name, args in tool_calls
        ]
        if self.managed:
            steps.append([{"retrieve": retrieve, "task": task_index}])
        steps.append([{"text": TASKS[task_index][1], "expected": TASKS[task_index][1]}])
        self.plan.extend(steps)

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        tokens = await self.count_tokens(messages, tool_specs, system_prompt)
        self.calls.append({"input_tokens": tokens, "summary": tool_specs is None})
        if tool_specs is None:  # the summarizer
            step = [{"text": "## Conversation Summary\n* earlier tasks and their answers"}]
        else:
            step = self.plan.pop(0)
        if "expected" in step[0]:
            # Offline "correct" = the answer is in what the model was given.
            self.calls[-1]["reachable"] = step[0]["expected"] in json.dumps(messages)
        yield {"messageStart": {"role": "assistant"}}
        stop = "end_turn"
        for block in step:
            if "tool" in block or "retrieve" in block:
                stop = "tool_use"
                if "tool" in block:
                    name, payload = block["tool"], block["input"]
                else:
                    name, payload = (
                        RETRIEVAL_TOOL_NAME,
                        {"reference": self._reference(messages, block), **block["retrieve"]},
                    )
                use_id = f"tooluse_{len(self.calls):03d}"
                yield {
                    "contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": use_id}}}
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}}
                yield {"contentBlockStop": {}}
            else:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": stop}}
        usage = {"inputTokens": tokens, "outputTokens": 10, "totalTokens": tokens + 10}
        yield {"metadata": {"usage": usage, "metrics": {"latencyMs": 0}}}

    @staticmethod
    def _reference(messages, block) -> str:
        """The reference of the offloaded result the task refers to (first of its tool)."""
        refs = []
        for message in messages:
            for item in message.get("content", []):
                text = (item.get("toolResult") or {}).get("content", [{}])[0].get("text", "")
                if text.startswith("[Offloaded:"):
                    refs.append(text.rsplit("[Stored references:]", 1)[1].split()[0])
        index = block["task"] if block["task"] < 3 else 0
        return refs[index] if index < len(refs) else (refs[-1] if refs else "missing")


def run_offline(window: int, repeat: int = 1) -> dict[str, list[dict[str, Any]]]:
    results = {}
    for label, managed in (("off", False), ("on", True)):
        model = ScriptedModel(managed)
        with tempfile.TemporaryDirectory() as stash:
            agent = NXPAgent(
                model=model,
                tools=TOOLS,
                system_prompt=SYSTEM,
                stimulus_classifier=RuleBasedClassifier(),
                context_management=ContextManagementSettings(
                    enabled=managed, context_window_tokens=window
                ),
                context_stash_dir=stash,
            )
            rows = []
            work = [(r, i, prompt) for r in range(repeat) for i, (prompt, _) in enumerate(TASKS)]
            for turn, (_round, index, prompt) in enumerate(work, 1):
                model.queue(index)
                before = len(model.calls)
                with contextlib.redirect_stdout(io.StringIO()):
                    agent(prompt)
                calls = model.calls[before:]
                rows.append(
                    {
                        "task": turn,
                        "summaries": sum(1 for c in calls if c["summary"]),
                        "calls": len(calls),
                        "input_tokens": sum(c["input_tokens"] for c in calls),
                        "last_call": calls[-1]["input_tokens"],
                        "correct": bool(calls[-1].get("reachable")),
                    }
                )
            results[label] = rows
    return results


# --------------------------------------------------------------------- live


class CallBudget(Exception):
    pass


def run_live(model_id: str, region: str, max_calls: int) -> dict[str, list[dict[str, Any]]]:
    from strands.models.bedrock import BedrockModel

    used = {"calls": 0}
    results: dict[str, list[dict[str, Any]]] = {}
    for label, managed in (("off", False), ("on", True)):
        model = BedrockModel(model_id=model_id, region_name=region, max_tokens=512)
        send = model.client.converse_stream

        def counted(_send=send, **request):
            if used["calls"] >= max_calls:
                raise CallBudget(f"model call budget of {max_calls} reached")
            used["calls"] += 1
            return _send(**request)

        model.client.converse_stream = counted
        rows: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory() as stash:
            agent = NXPAgent(
                model=model,
                tools=TOOLS,
                system_prompt=SYSTEM,
                stimulus_classifier=RuleBasedClassifier(),
                context_management=ContextManagementSettings(enabled=managed),
                context_stash_dir=stash,
            )
            for index, (prompt, expected) in enumerate(TASKS):
                start = used["calls"]
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = agent(prompt)
                except CallBudget as stop:
                    print(f"[{label}] task {index + 1}: {stop}", file=sys.stderr)
                    break
                usage = result.metrics.accumulated_usage
                text = str(result).strip()
                rows.append(
                    {
                        "task": index + 1,
                        "calls": used["calls"] - start,
                        "input_tokens": usage.get("inputTokens", 0),
                        "output_tokens": usage.get("outputTokens", 0),
                        "correct": expected in text,
                        "answer": text[:80].replace("\n", " "),
                        "retrieved": sum(
                            1
                            for message in agent.agent.messages
                            for block in message.get("content", [])
                            if (block.get("toolUse") or {}).get("name") == RETRIEVAL_TOOL_NAME
                        ),
                    }
                )
        results[label] = rows
    print(f"model calls used: {used['calls']} / {max_calls}", file=sys.stderr)
    return results


# --------------------------------------------------------------------- report


def table(results: dict[str, list[dict[str, Any]]], live: bool) -> str:
    lines = []
    header = "| condition | task | calls | input tokens | correct |"
    if live:
        header = "| condition | task | calls | input tokens | output tokens | correct | answer |"
    lines += [header, "|" + "---|" * (header.count("|") - 1)]
    for label, rows in results.items():
        for row in rows:
            if live:
                lines.append(
                    f"| {label} | {row['task']} | {row['calls']} | {row['input_tokens']:,} | "
                    f"{row['output_tokens']:,} | {'yes' if row['correct'] else 'no'} | {row['answer']} |"
                )
            else:
                lines.append(
                    f"| {label} | {row['task']} | {row['calls']} | {row['input_tokens']:,} | "
                    f"{'yes' if row['correct'] else 'no'} |"
                )
        total = sum(row["input_tokens"] for row in rows)
        calls = sum(r["calls"] for r in rows)
        dollars = total * INPUT_PRICE / 1e6
        if live:
            out = sum(r["output_tokens"] for r in rows)
            dollars += out * OUTPUT_PRICE / 1e6
            lines.append(
                f"| {label} | **total** | {calls} | **{total:,}** | {out:,} | "
                f"${dollars:.4f} (in+out) | |"
            )
        else:
            lines.append(f"| {label} | **total** | {calls} | **{total:,}** | ${dollars:.4f} (in) |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live", action="store_true", help="call Bedrock (counts against --max-calls)"
    )
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--model-id", default="global.anthropic.claude-sonnet-5-5")
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--window", type=int, default=200_000, help="offline context window")
    parser.add_argument("--repeat", type=int, default=1, help="offline: run the tasks N times")
    parser.add_argument("--json", type=Path, help="also write the raw rows here")
    args = parser.parse_args(argv)
    if args.live:
        results = run_live(args.model_id, args.region, args.max_calls)
    else:
        results = run_offline(args.window, args.repeat)
    print(table(results, args.live))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
