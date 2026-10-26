# Quickstart

## Install

StiraCore targets Python 3.11. From the repository root (macOS may have no
`python` command before the venv is active, so create it with `python3`):

```bash
python3 -m venv .venv      # Windows: py -3.11 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .
# Optional extras:
pip install -e ".[jev]"
```

## Run the engine without a model

`StiraEngine` and `NeuralState` work without any model or credentials
(adapted from `examples/basic_agent.py`):

```python
from stiracore.core import BreakLevel, NeuralState, StiraConfig, StiraEngine

clock = [1000.0]


def now():
    return clock[0]


def on_breaker(level: BreakLevel, message: str) -> None:
    print(f"[BREAKER] {message}")


state = NeuralState(
    trust=0.9,
    curiosity=0.8,
    vigilance=0.1,
    attachment=0.9,
    stability=0.85,
    satisfaction=0.7,
    last_decay_check=now(),
    last_stimulus_time=now(),
)
config = StiraConfig(stability_warning=0.5, stability_critical=0.3)
engine = StiraEngine(state=state, config=config, on_breaker=on_breaker, now_fn=now)

for stimulus in ["connection", "achievement", "uncertainty"]:
    clock[0] += 120
    level = engine.pre_turn()
    result = engine.post_turn(stimulus, intensity=0.8)
    print(level.value, f"stability={result['stability']:.3f}")
    engine.record_agreement(True)

print("L3 modify allowed:", engine.check_layer_boundary(3))
print("blocked:", engine.is_blocked())

state.save("quickstart_state.json")
loaded = NeuralState.load("quickstart_state.json")
print("round-trip match:", loaded.stability == state.stability)
```

## Run one turn with NXPAgent

`NXPAgent` wraps a Strands `Agent`. With `model_id` and `region_name` it uses
Amazon Bedrock (the default), which needs AWS credentials with Bedrock access.
Keep credentials in environment variables. Call the agent directly to run a turn:

```python no-run
from stiracore import NXPAgent

agent = NXPAgent(
    model_id="<your-bedrock-model-id>",
    region_name="us-east-1",
    agent_id="quickstart",
    system_prompt="You are a concise assistant.",
)
result = agent("Remember that I prefer concise answers.")
print(result)
print(agent.last_turn_summary)
```

### Without AWS: choose another provider

Pass `model=` with a `ModelSpec` instead of `model_id`/`region_name`. Building
the agent does not contact the model; see [providers.md](providers.md) for
Ollama, OpenAI, Anthropic, and the other providers:

```python
from stiracore import ModelSpec, NXPAgent

local = NXPAgent(
    model=ModelSpec("llamacpp", "local", {"base_url": "http://127.0.0.1:8080"}),
    agent_id="quickstart-local",
    max_output_tokens=512,
)
print(local.provider, local.model_id)  # llamacpp local
# result = local("Hello!")  # needs a running llama-server
```

Use a stable `agent_id` to keep agents separate. Other keyword arguments include
`state`, `config`, `tools`, `system_prompt_provider`, `session_manager`,
`read_only_paths`, `checkpoint_store`, and `max_output_tokens`. `NXPAgent` does
not take a memory directory or a SOUL path; use `ContextAssembler` for those.

## Assemble context

The signature is
`ContextAssembler(agent_id, memory_store, engine, soul_path=None, *, max_tokens=8000, ...)`.
`max_tokens` is an estimate, not a tokenizer count ([token-estimate.md](token-estimate.md)).
`memory_store` is any object with `list_entries(layer, limit)` that returns
entries newest first. Each entry needs a `content` field. This example uses a
small in-memory store and reuses `engine` from the block above:

```python
from pathlib import Path

from stiracore import ContextAssembler


class ListMemoryStore:
    def __init__(self, entries):
        self.entries = entries

    def list_entries(self, layer, limit=20):
        layer_name = getattr(layer, "value", layer)
        return [e for e in self.entries if e["layer"] == layer_name][:limit]


Path("soul.md").write_text("I am a careful, concise assistant.\n", encoding="utf-8")
store = ListMemoryStore(
    [
        {"layer": "procedural", "content": "Answer concisely."},
        {"layer": "episodic", "content": "User prefers concise answers."},
    ]
)
assembler = ContextAssembler("quickstart", store, engine, soul_path=Path("soul.md"))
print(assembler.build_startup_context())
print(assembler.build_turn_context())
system_prompt = assembler.build_system_prompt()
assert "concise" in system_prompt
```

To use this context with an agent, pass the bound method as
`NXPAgent(..., system_prompt_provider=assembler.build_system_prompt)`. That
setup needs Bedrock, so the agent itself is not shown running here. When
`NXPAgent` injects identity itself, construct the assembler with
`include_identity=False`. This avoids adding the identity notice twice.

## Persist state

The signature is `StatePersistence(state, path)`. `save()` writes the state
atomically. `load()` restores fields into the same object, so an engine holding
that object sees the restored values:

```python
from pathlib import Path

from stiracore import NeuralState, StatePersistence

persisted = NeuralState()
persistence = StatePersistence(persisted, Path(".state/quickstart.json"))
persistence.load()  # missing file: keeps in-memory defaults
persistence.save()

restored = NeuralState()
StatePersistence(restored, Path(".state/quickstart.json")).load()
assert restored.stability == persisted.stability
```

`persistence.auto_save_hook()` returns a callback that saves the state. Call it
after `engine.post_turn`. Keep state files private and out of version control.

## Dashboard

Start the dashboard server from the repository root. It needs Flask, from the
`dashboard` extra. Set the port with `STIRA_PORT`:

```bash
pip install -e ".[dashboard]"
STIRA_PORT=8080 python dashboard/stira_server.py
```

Bind locally unless you have configured authentication and access controls.
