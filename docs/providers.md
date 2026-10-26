# Model providers

StiraCore runs on any model provider that the Strands Agents SDK supports.
**Amazon Bedrock is the default** (and what existing `model_id` / `region_name`
code uses), but no part of the harness — Neural State, breaker, memory,
classifiers, UI — requires AWS. The test suite and CI run without any cloud
account.

| Provider | `ModelSpec(provider=…)` | Install | Credentials / endpoint |
|---|---|---|---|
| Amazon Bedrock (default) | `bedrock` | core | AWS credential chain (profile / SSO / role / env). See [setup.md](setup.md), [bedrock-iam-policy.md](bedrock-iam-policy.md) |
| Anthropic API | `anthropic` | `pip install -e '.[anthropic]'` | `ANTHROPIC_API_KEY` |
| OpenAI (Chat Completions) and OpenAI-compatible servers | `openai` | `.[openai]` | `OPENAI_API_KEY`; `base_url` for compatible servers (LM Studio, vLLM, …) |
| OpenAI Responses API | `openai_responses` | `.[openai]` | `OPENAI_API_KEY` |
| Ollama (local) | `ollama` | `.[ollama]` | `base_url`, default `http://localhost:11434` |
| llama.cpp `llama-server` (local) | `llamacpp` | core | `base_url`, default `http://localhost:8080` |
| Google Gemini | `gemini` | `.[gemini]` | `GOOGLE_API_KEY` / `GEMINI_API_KEY` (google-genai) |
| LiteLLM | `litellm` | `.[litellm]` | per LiteLLM backend |
| Mistral | `mistral` | `.[mistral]` | `MISTRAL_API_KEY` |
| Writer | `writer` | `.[writer]` | `WRITER_API_KEY` |
| Llama API (deprecated upstream) | `llamaapi` | `.[llamaapi]` | `LLAMA_API_KEY` |
| SageMaker endpoint | `sagemaker` | `.[sagemaker]` | AWS credential chain; `model_id` is the endpoint name |

`pip install -e '.[providers]'` installs the Phase 1 SDKs (Anthropic, OpenAI,
Ollama) at once. If an SDK is missing, `create_model` raises
`ProviderUnavailableError` with the exact command, for example
`pip install 'stiracore[openai]'`.

Keep API keys in the environment (or your OS keychain → environment). Never put
them in code, config JSON, or Git.

## Quickstart without AWS

### Local: Ollama

```bash
pip install -e '.[ollama]'
ollama pull llama3.2:3b        # any chat model you have
```

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("ollama", "llama3.2:3b", {"base_url": "http://127.0.0.1:11434"}))
print(agent("Hello!"))
```

### Local: llama.cpp

No extra package is needed; start `llama-server -m model.gguf --port 8080 --jinja`
(`--jinja` enables tool calling for most chat templates), then:

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("llamacpp", "local", {"base_url": "http://127.0.0.1:8080"}))
print(agent("Hello!"))
```

### Hosted API: OpenAI / Anthropic

```bash
pip install -e '.[openai]'          # or '.[anthropic]'
export OPENAI_API_KEY=...           # PowerShell: $env:OPENAI_API_KEY = "..."
```

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("openai", "gpt-5-mini"))
# agent = NXPAgent(model=ModelSpec("anthropic", "claude-sonnet-4-5"))
print(agent("Hello!"))
```

The steps are the same on Linux, macOS, and Windows; only the shell syntax for
environment variables differs.

## Running without AWS (memory, distillation, tools)

Everything except the chat model is local by default. A setup with no AWS
account at all:

| Part | Without AWS |
|---|---|
| Chat model | any provider above (`provider` in `gateway-config.json`, e.g. `llamacpp` / `ollama` / `openai`) |
| Emotion classifier | `rule` or `sokudan` — local, provider-independent |
| Memory (Episodic / Semantic / Procedural) | Stira's `MemoryStore` (Markdown files + SQLite index) and its `memory_read` / `memory_write` / `memory_search` / `memory_list` tools — the source of truth for recall |
| Harvest / distillation | `distiller_mode: "rule"` (default): deterministic AmygdalaPass, **no model call, zero cost** |
| LLM distiller (optional) | `distiller_mode: "llm"` with `distiller_provider` / `distiller_model_id` / `distiller_base_url` / `distiller_api_key_env` in `agent-config.json` (same reference-only credential rules as the chat model). Default: Bedrock Haiku |
| Standard Strands `memory` tool | **Bedrock-only, optional**: backed by a Bedrock Knowledge Base and loaded only when `STRANDS_KNOWLEDGE_BASE_ID` is set; skipped otherwise. Not needed for Stira memory |

Example `agent-config.json` fragment for an LLM distiller on a local llama.cpp server:

```json
{
  "distiller_mode": "llm",
  "distiller_provider": "llamacpp",
  "distiller_model_id": "local",
  "distiller_base_url": "http://127.0.0.1:8080"
}
```

In code, `LLMDistiller` takes a `ModelSpec`, a Bedrock model ID string (legacy), or
any Strands `Model` instance (for example the offline fake in `tests/provider_fakes.py`):
`LLMDistiller(ModelSpec("ollama", "llama3.2:3b"))`. When switching
`distiller_provider`, also set `distiller_model_id` (the default is a Bedrock ID).

## Configure the chat server (credentials by reference)

`dashboard/stira_server.py` and the chat integrations read the provider from
`.local/stira/gateway-config.json`. The file stores **environment-variable
names only** — never a key:

```json
{
  "provider": "openai",
  "model_id": "gpt-5-mini",
  "api_key_env": "OPENAI_API_KEY",
  "base_url": null
}
```

| Field | Meaning |
|---|---|
| `provider` | `bedrock` (default) or any provider in the table above |
| `model_id` | provider's model name (`STIRA_MODEL` overrides) |
| `base_url` | optional endpoint (Ollama / llama.cpp / OpenAI-compatible / Bedrock VPC endpoint). Must be `http(s)://host…` without user:password, query, or fragment |
| `api_key_env` | NAME of the variable that holds the API key. Omit to use the SDK default (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …). For Bedrock it selects a Bedrock API key instead of the AWS chain |
| `organization_env` | OpenAI only: NAME of the variable with the organization ID |

Environment overrides: `STIRA_PROVIDER`, `STIRA_MODEL`, `STIRA_BASE_URL`,
`STIRA_API_KEY_ENV`. Per agent, `agent-config.json` may set
`provider_override` (requires `model_override`) with its own
`base_url_override` / `api_key_env_override`; gateway endpoint/key references
are then not inherited.

Guarantees (tested in `tests/test_provider_credentials.py` with a canary key):

- A pasted key in `api_key_env`/`provider`/`base_url` is rejected, and the error
  message does not repeat it.
- The key value is read from the environment only when the client is built and
  is passed only to that client: it never appears in config files, `/api/config`,
  `/api/gateway/status`, chat SSE, turn replay (`/api/turns/...`), logs, or stdout.
- Status APIs report readiness only: `credentials = {kind, env, configured, warnings}`
  where `env` is the variable name and `configured` is `true`/`false`
  (`null` for the AWS chain when only the SDK can tell, e.g. an instance role).
- A missing key fails fast at startup with the variable's name, e.g.
  `Model provider 'openai' needs an API key in the environment variable OPENAI_API_KEY, which is not set`.
- A non-loopback `base_url` for Ollama/llama.cpp, or an API key over plain HTTP to
  a non-loopback host, logs a warning.

Least privilege: use a project-scoped key with only inference permissions
(OpenAI project key, Anthropic workspace key, Gemini API key restricted to the
Generative Language API). For Bedrock, use the invocation-only policy in
[bedrock-iam-policy.md](bedrock-iam-policy.md). Local providers need no key;
keep them on `127.0.0.1`.

## Settings page: provider, model and connection test

`/settings` → **Model selection** lists every provider with its suggested models
and capability badges (vision / tools / thinking; "capabilities unknown" for models
outside the table). Credentials are shown as status only — configured / not
configured / not needed (local) / AWS chain — plus the variable *name*; values are
never sent to the browser. Pick a model (or type any model ID) and press
**Use this model**: the choice is saved to `agent-config.json`
(`model_override` / `provider_override` / `base_url_override` /
`api_key_env_override`) and the chat server switches **from the next turn**
(the page shows "Selected" vs "Running"). `STIRA_PROVIDER` / `STIRA_MODEL` /
`STIRA_BASE_URL` / `STIRA_API_KEY_ENV` in the environment take precedence, and the
page says so. A remote `base_url` for an API provider can be set only in
`gateway-config.json`, so the browser cannot route a key to an arbitrary host.

**Connection test** sends one tiny request (prompt `ping`, no tools or system
prompt, at most 16 output tokens, 20 s timeout, one test at a time) and reports
`ok` or a reason code (`missing_credential`, `provider_unavailable`, `auth_failed`,
`model_not_found`, `unreachable`, `timeout`, …) with the HTTP status and exception
class only — never the provider's message, headers, or key. Local providers cost
nothing; hosted APIs may bill a few tokens. Both POSTs use the CSRF token and an
`Idempotency-Key` (#185).

## ModelSpec reference

`ModelSpec(provider, model_id, options)`; `options` is passed to the Strands
provider class after a small normalisation:

- `max_tokens` — output budget. `NXPAgent(max_output_tokens=…)` supplies the
  default; an explicit value in `options` wins. It is mapped to each provider's
  own key (`max_tokens`, `params.max_tokens`, `params.max_output_tokens`,
  `max_completion_tokens`). For OpenAI reasoning models that require
  `max_completion_tokens`, set `{"params": {"max_completion_tokens": N}}`.
- `temperature`, `top_p`, `stop`, `seed` — moved into `params` for providers
  that only accept `params` (OpenAI, Responses, Gemini, LiteLLM, llama.cpp,
  Anthropic).
- `base_url` — client endpoint (OpenAI-compatible / Anthropic), Ollama `host`,
  or llama.cpp server URL.
- `client_args` — passed to the provider's HTTP client unchanged.
- `region_name` — Bedrock region (`ModelSpec.bedrock(model_id, region_name=…)`).

`ModelSpec.parse("ollama:llama3.2:3b")` reads a `provider:model` reference;
strings that do not start with a known provider (all Bedrock model IDs, which
may contain `:`) stay Bedrock. Role routing in `model_factory` accepts
`ModelSpec` values, while plain strings keep meaning Bedrock model IDs.

You can also pass any Strands `Model` instance: `NXPAgent(model=MyModel(...))`.

## Capabilities and usage

- `stiracore.providers.capabilities_for(spec)` returns `ModelCapabilities`
  (`vision`, `tools`, `thinking`, `prompt_cache`, `thinking_uses_output_budget`,
  `max_output`, `thinking_reserve`, `context_window`, `known`, `source`;
  `thinking_reserve` = room the automatic output cap adds for thinking, #416). Unknown models get
  conservative defaults (`False` / `None`, `known=False`). Every Strands adapter
  transports the same image block, so vision is a property of the model, not the
  provider. Declare a local model's abilities with
  `ModelSpec(..., {"capabilities": {"vision": True}})`; `NXPAgent.capabilities`
  exposes the result.
- `/api/chat` rejects image attachments (HTTP 400, `capability: "vision"`) for
  models *known* to be text-only; unknown models behave as before.
- `normalize_strands_usage(usage, provider)` / `normalize_raw_usage(provider, raw)`
  produce `NormalizedUsage` with `input_tokens` = **uncached** input plus separate
  `cache_read_tokens` / `cache_write_tokens` (OpenAI, Gemini, LiteLLM and llama.cpp
  count cached tokens inside their prompt total; Bedrock and Anthropic report them
  separately). Unreported fields stay `None`.
- `estimate_cost(usage, Pricing(...))` returns `None` whenever a used bucket has
  no price, so unknown cost is shown as "unknown", never `$0`.
- Opt-in prompt caching (Bedrock Claude only) and the static/volatile prompt
  split: [prompt-cache.md](prompt-cache.md).

## Tests and live smoke

- The default suite uses `tests/provider_fakes.py` (a real Strands `Model`
  subclass that replays scripted turns) for the provider contract: text, tool
  calls, streaming, images, `max_tokens`, errors, and the chat API pipeline.
  A root `conftest.py` guard fails any test that would send a request to AWS.
- Live smoke is opt-in and skipped by default:

```bash
STIRA_LIVE_MODEL="llamacpp:local" STIRA_LIVE_BASE_URL="http://127.0.0.1:8080" \
STIRA_LIVE_TOOLS=1 pytest tests/test_provider_live.py -m live
```

`STIRA_LIVE_MODEL` takes the same `provider:model` form (a bare Bedrock ID uses
Bedrock and your AWS credentials).
