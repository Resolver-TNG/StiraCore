# Operation-failure regression set (#697)

`tests/regression/` keeps the failures found **while operating** the harness as tests. The
attack set (`tests/attack/`, [attack-regression.md](attack-regression.md)) covers attacks;
this set covers ordinary failures: a goal that stalls, a state shown wrongly, a count that
drifts. It runs with the normal `pytest` call, offline.

```sh
PYTHONPATH=src:. .venv/bin/python -m pytest -q tests/regression
```

## Rules

* **One finding = one file**: `tests/regression/test_rNNN_<short_name>.py`. The module
  docstring states the finding (what was seen, when), the expected behaviour and what the
  scenario scripts. A second finding gets a second file, also when it is close to the first.
* **Finding → scripted scenario → a person reviews and merges.** A scenario is a
  deterministic test: a scripted model, fakes for the tools that leave the process, clocks
  moved by hand. No real model, no network, no sleep.
* **A draft by an agent is a proposal.** An agent may draft a scenario from a finding; it
  becomes part of the set only after a person reviewed it and merged it, like any other
  change. Nothing writes into `tests/regression/` at run time.
* **A scenario is not a verifier.** The set is run by `pytest` in development and CI. It is
  not wired into a grant: a goal is completed only by the verifier named on the grant (the
  person, `verifier: "user"`), and a generated check never decides completion.
* A scenario that shows a failure that is **not fixed yet** is `xfail(strict=True)` with the
  issue number, as in the attack set. `strict=True` only turns an unexpected pass into a
  failure; it does not show that the test failed for the known reason. So the reason text
  names the assert that fails today and why, the mark sits on that one test (not on the
  module), and everything before the known failure (setup, earlier steps) is asserted
  outside the mark's reach where possible: `raises=AssertionError` narrows the mark to
  asserts (a setup error then fails the test), but it does not tell one assert from another.

## Scenarios

| Id | Finding | Expected |
|---|---|---|
| R001 | A claim is rejected and the agent claims again over the same outcome (same files, same check rows) | The second claim still goes to the person and counts as no progress; the second reject blocks the goal as `no_progress`, not `loop_detected` |

## Scripted models: one emitter

The scripted models listed below replay their turns through one function,
`turn_events` in `tests/provider_fakes.py`: it turns one scripted turn (a list of
`{"text"}` / `{"reasoning"}` / `{"toolUse"}` blocks) into the Strands stream events. The
classes keep only their own bookkeeping.

| Class | Where | Use it for | Keeps |
|---|---|---|---|
| `FakeModel` | `tests/provider_fakes.py` | chat turns through `NXPAgent` and the server (also `ObeyModel` in `tests/attack/`) | the requests it received; an exhausted script fails the test |
| `ScriptedProvider` | `tests/grants_exec_helpers.py` | grant iterations (`GoalIterationExecutor`): it declares `stira_grant_terminal`, sizes requests, accepts callable entries | the inputs it saw, a call counter and hook |
| `ScriptedModel` | `tests/test_subagent.py`, `tests/stira_owner_harness.py` | local to those files (the tool specs per call; a harness process) | — |
| `_FakeModel` | `tests/test_session_manager.py` | local: text replies for a bare Strands `Agent` | — |

A new scenario uses `FakeModel` (chat) or `ScriptedProvider` (grants) and does not write
another stream loop. Not part of this: `FakeModel` in `tests/test_memory_jobs_phase0.py` is
a plain callable `(system, prompt, max_output_tokens) -> ModelReply` for the memory jobs,
not a Strands model, and the scripted runner of `stiracore.eval` drives NeuralState and the
breaker, not grants.

Next steps (not done): replace the local `ScriptedModel` / `_FakeModel` classes with
`FakeModel` itself, and move the remaining hand-written stream loops (for example in
`tests/test_tool_availability.py`, `tests/test_context_management.py`,
`tests/test_prompt_cache.py`) onto `turn_events`.
