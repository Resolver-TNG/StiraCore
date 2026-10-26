#!/usr/bin/env python3
"""StiraCore リゾルバ デモ — 対話モード"""

import sys
import time
from pathlib import Path

# StiraCore imports
from stiracore import (
    ContextAssembler,
    InMemoryEventSink,
    MemoryLayer,
    MemoryStore,
    NeuralState,
    NXPAgent,
    StatePersistence,
    StiraConfig,
    StiraEngine,
)
from stiracore.tools.episodic_formatter import EpisodicFormatter, record_episode

EPISODIC_FORMATTER = EpisodicFormatter(user_label="マスター", agent_label="リゾルバ")

SOUL_PATH = Path(__file__).parent.parent / "examples" / "personas" / "resolver" / "SOUL.md"
DATA_DIR = Path(__file__).parent.parent / ".local" / "stira"


def setup():
    """リゾルバ環境をセットアップ"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state_path = DATA_DIR / "neural-state.json"
    memory_dir = DATA_DIR / "memory"
    memory_dir.mkdir(exist_ok=True)

    # Neural State (永続化)
    state = NeuralState()
    persistence = StatePersistence(state, state_path)
    persistence.load()  # 前回の状態があれば復元

    # Engine
    config = StiraConfig()
    engine = StiraEngine(state=state, config=config)

    # Memory
    store = MemoryStore(memory_dir, "stira")

    # Context Assembler (SOUL.md読み込み)
    assembler = ContextAssembler(
        agent_id="stira",
        memory_store=store,
        engine=engine,
        soul_path=SOUL_PATH,
        max_tokens=4000,
    )

    # Observability
    sink = InMemoryEventSink()

    # Model (Haiku for demo — 低コスト)
    system_prompt = assembler.build_system_prompt()

    agent = NXPAgent(
        model_id="global.anthropic.claude-sonnet-5",
        region_name="ap-northeast-1",
        state=state,
        config=config,
        system_prompt=system_prompt,
    )

    return agent, engine, state, persistence, store, assembler, sink


def print_state(state):
    """Neural State を表示"""
    dims = {
        "trust": state.trust,
        "curiosity": state.curiosity,
        "vigilance": state.vigilance,
        "attachment": state.attachment,
        "stability": state.stability,
        "satisfaction": state.satisfaction,
    }
    bars = " ".join(
        f"{k[:3]}={'█' * int(v * 10)}{' ' * (10 - int(v * 10))}|{v:.2f}" for k, v in dims.items()
    )
    print(f"  [{bars}]")


def main():
    print("=" * 60)
    print("  StiraCore v0.1 — リゾルバ デモ")
    print("  SOUL: " + str(SOUL_PATH))
    print("=" * 60)
    print()

    if not SOUL_PATH.exists():
        print(f"ERROR: SOUL.md not found at {SOUL_PATH}")
        sys.exit(1)

    print("セットアップ中...")
    agent, engine, state, persistence, store, _assembler, _sink = setup()
    print(f"SOUL.md: {SOUL_PATH.stat().st_size} bytes loaded")
    print(f"Neural State: stability={state.stability:.2f}")
    print("Model: Haiku 4.5 (ap-northeast-1)")
    print()
    print("対話開始。'quit' で終了、'state' でNeural State表示、'memory' で記憶一覧")
    print("-" * 60)
    print()

    turn = 0
    while True:
        try:
            user_input = input("マスター > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n終了します。")
            break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "state":
            print_state(state)
            continue
        if user_input.lower() == "memory":
            for layer in MemoryLayer:
                entries = store.list_entries(layer, limit=3)
                print(f"  [{layer.value}] {len(list(entries))} entries")
            continue

        turn += 1
        t0 = time.time()
        try:
            # pre_turn
            result = engine.pre_turn()
            print(f"  [breaker: {result.level.value}]")

            # Run agent
            response = agent(user_input)
            elapsed = time.time() - t0

            # post_turn
            engine.post_turn(stimulus="curiosity", intensity=0.1)
            persistence.save()

            # Display
            print(f"\nリゾルバ > {response}")
            print(f"  [{elapsed:.1f}s | turn {turn} | stability={state.stability:.2f}]")
            print()

            # Log to memory (#77: deterministic summary, never raises)
            record_episode(
                store,
                user_input,
                response,
                formatter=EPISODIC_FORMATTER,
                key=f"turn-{turn}",
                source="demo",
                extra_metadata={"turn": turn},
            )

        except RuntimeError as e:
            print(f"\n⚠️ ブレーカー作動: {e}")
            print_state(state)
        except Exception as e:  # noqa: BLE001 - isolate optional plugin or batch-job failures
            print(f"\n❌ エラー: {e}")

    # Save final state
    persistence.save()
    print(f"\n状態を保存しました: {DATA_DIR}")
    print("お疲れ様でした。— リゾルバ")


if __name__ == "__main__":
    main()
