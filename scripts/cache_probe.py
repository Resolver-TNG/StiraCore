#!/usr/bin/env python3
"""Measure prompt-cache reads/writes over a fixed 12-turn conversation (#254).

Drives a running ``dashboard/stira_server.py`` through ``POST /api/chat``
(CSRF token from ``GET /api/csrf``), reads each turn's usage from the SSE
``turn`` event (``input_tokens`` / ``cache_read_tokens`` / ``cache_write_tokens``,
which come from the model's ``metadata.usage``) and prints a Markdown table
with an input-side cost estimate.

Every turn is a real model call. Bedrock is billed per call: 12 turns per run.

Reproduce (from a worktree, on a port nobody else uses, e.g. 5611)::

    WT=$(pwd)  # the worktree root
    PY=~/projects/StiraCore/.venv/bin/python
    rm -rf "$WT/.local/stira"   # fresh history/episodic memory for each condition
    STIRA_PORT=5611 STIRA_PROMPT_CACHE=1 \\
      STIRA_MODEL=global.anthropic.claude-sonnet-5-5 AWS_REGION=ap-northeast-1 \\
      PYTHONPATH="$WT/src:$WT" "$PY" dashboard/stira_server.py > /tmp/stira.log 2>&1 &
    "$PY" scripts/cache_probe.py --port 5611 --label on --json /tmp/probe-on.json
    kill %1
    # Repeat with STIRA_PROMPT_CACHE=0 (and a fresh .local/stira) for the baseline.

Cost assumptions (override with flags): input $2/Mtok (Sonnet 5.5); cache reads
are shown at both 0.05x input (Anthropic's Sonnet 5.5 rate) and 0.1x (the usual
rate); cache writes cost 1.25x input for a 5m TTL and 2x for 1h (``--ttl``).
Bedrock's own prices are not confirmed here; check the price list before quoting.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

PROMPTS = (
    "Transformer論文(Attention Is All You Need)の核心を、自己注意の計算量の観点も含めて5行で説明して。",
    "スケーリング則で、Kaplan(2020)とChinchilla(2022)は何が違うの？",
    "MoEが推論コストを下げる仕組みと、負荷分散損失が必要な理由を教えて。",
    "RLHFとDPOの違いを、目的関数の骨子で比較して。",
    "Mambaなどの状態空間モデルは、Transformerの何を解決しようとしたの？",
    "In-context learningが起きる理由について、有力な仮説を3つ挙げて。",
    "Constitutional AIの手順と限界を整理して。",
    "推論時スケーリング(長いCoTやbest-of-N)の利点と落とし穴は？",
    "KVキャッシュとプロンプトキャッシュの違いを、実装者向けに説明して。",
    "ここまでの議論を踏まえて、自己記録するエージェントの評価指標を3つ提案して。",
    "その3つのうち、最も検証しやすいものの実験計画を書いて。",
    "最後に、今日の議論の要点を3行でまとめて。",
)


def sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            try:
                events.append((name, json.loads(data)))
            except json.JSONDecodeError:
                continue
    return events


def count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def run(base: str, prompts, timeout: float) -> list[dict]:
    with urllib.request.urlopen(f"{base}/api/csrf", timeout=30) as response:
        token = json.load(response)["token"]
    rows = []
    for index, prompt in enumerate(prompts, 1):
        started = time.monotonic()
        request = urllib.request.Request(
            f"{base}/api/chat",
            data=json.dumps({"message": prompt}).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": token},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        events = sse_events(body)
        turn = next((data for name, data in events if name == "turn"), None)
        error = next((data.get("error") for name, data in events if name == "error"), None)
        row = {
            "turn": index,
            "ok": turn is not None,
            "sec": round(time.monotonic() - started, 1),
            "input": count((turn or {}).get("input_tokens")),
            "output": count((turn or {}).get("output_tokens")),
            "cache_read": count((turn or {}).get("cache_read_tokens")),
            "cache_write": count((turn or {}).get("cache_write_tokens")),
            "error": error,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), file=sys.stderr, flush=True)
        if turn is None:
            break
    return rows


def cost(row: dict, price: float, read_x: float, write_x: float) -> float:
    per_token = price / 1_000_000
    return per_token * (row["input"] + row["cache_read"] * read_x + row["cache_write"] * write_x)


READ_RATES = (0.05, 0.1)
WRITE_RATES = {"5m": 1.25, "1h": 2.0}


def table(rows: list[dict], label: str, price: float, read_rates, write_x: float) -> str:
    read_rates = tuple(read_rates)
    cost_heads = " | ".join(f"$ (read {rate:g}x)" for rate in read_rates)
    lines = [
        f"### {label}",
        "",
        f"input-side cost: input ${price:g}/Mtok, write {write_x:g}x",
        "",
        f"| turn | input | cache read | cache write | hit rate | {cost_heads} |",
        "|---:|---:|---:|---:|---:|" + "---:|" * len(read_rates),
    ]
    for row in rows:
        prompt = row["input"] + row["cache_read"] + row["cache_write"]
        hit = f"{row['cache_read'] / prompt:.1%}" if prompt else "—"
        costs = " | ".join(f"{cost(row, price, rate, write_x):.4f}" for rate in read_rates)
        lines.append(
            f"| {row['turn']} | {row['input']:,} | {row['cache_read']:,} | {row['cache_write']:,}"
            f" | {hit} | {costs} |"
        )
    total = {key: sum(row[key] for row in rows) for key in ("input", "cache_read", "cache_write")}
    prompt = sum(total.values())
    hit = f"{total['cache_read'] / prompt:.1%}" if prompt else "—"
    costs = " | ".join(f"{cost(total, price, rate, write_x):.4f}" for rate in read_rates)
    lines.append(
        f"| **total** | {total['input']:,} | {total['cache_read']:,} | {total['cache_write']:,}"
        f" | {hit} | {costs} |"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--label", default="run")
    parser.add_argument("--turns", type=int, default=len(PROMPTS))
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--json", help="write the per-turn rows to this file")
    parser.add_argument("--input-price", type=float, default=2.0, help="USD per Mtok")
    parser.add_argument("--ttl", choices=sorted(WRITE_RATES), default="5m")
    parser.add_argument("--write-x", type=float, help="override the TTL's write multiplier")
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= len(PROMPTS):
        parser.error(f"--turns must be between 1 and {len(PROMPTS)}")
    rows = run(f"http://{args.host}:{args.port}", PROMPTS[: args.turns], args.timeout)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"label": args.label, "rows": rows}, handle, ensure_ascii=False, indent=1)
    write_x = args.write_x if args.write_x is not None else WRITE_RATES[args.ttl]
    print(table(rows, args.label, args.input_price, READ_RATES, write_x))
    return 0 if rows and all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
