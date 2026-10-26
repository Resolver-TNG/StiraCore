# Installation guide

English | [日本語](install_ja.md)

Install from the repository root. This guide covers OS setup, tool availability,
and the two local web applications. For AWS credentials and model access, use
[Authentication & Environment](setup.md); for Python API examples, see the
[Quickstart](quickstart.md) and [README example](../README_ja.md#クイックスタート).

## 1. Prerequisites

- **Python 3.11+**, `pip`, and Git. Python 3.11/3.12 are the CI matrix; newer
  versions are not continuously tested. The optional Sokudan classifier needs
  **Python 3.11** (`jev` is skipped on 3.12+).
- Access to this GitHub repository. Authenticate Git normally; do not embed a
  token in the clone URL.
- For live chat: an AWS account with access to your selected Bedrock model.
  Follow [setup.md](setup.md) for AWS CLI v2, credentials, IAM, and region setup.
  The observation console does not require an AWS agent.
- A writable checkout for local configuration, state, and logs. Do not commit
  credentials, private memories, or generated state.

[pyproject.toml](../pyproject.toml) is the dependency source of truth. Flask is
**not a base dependency**: install the `dashboard` extra for either web application
(the `dev` extra also includes Flask, plus the test and lint tools).
The `harness` extra is only needed for the official `make_subagent` adapter.
The `ja-rules` extra (ML-Ask + pip-packaged IPADIC, no system MeCab) is only needed
for the opt-in Japanese rule classifier: [ja-rules-classifier.md](ja-rules-classifier.md).

## 2. Linux (Ubuntu / Debian)

CI uses `ubuntu-latest`; Debian and other distributions are not CI targets.
Ubuntu 24.04's Python 3.12 or Debian 12's Python 3.11 meet the requirement.
Check the version on older distributions before creating the environment.

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
python3 --version  # Must be 3.11 or newer

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
# Web applications and verification tools:
python -m pip install -e '.[dev]'
```

Use a virtual environment rather than system-wide `sudo pip` (distribution
Python may be externally managed). If a dependency must compile from source,
install the matching Python development headers and `build-essential`.
The optional `cron` tool requires the system `crontab` command and a running
cron service for jobs to execute; minimal containers may have neither.

## 3. macOS

Use a Homebrew Python rather than Apple's system Python. With Homebrew already
installed:

```bash
brew install python@3.11 git

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
# Web applications and verification tools:
python -m pip install -e '.[dev]'
```

The python.org installer for Python 3.11 works as well: create the venv with
`python3.11 -m venv .venv` instead. macOS may have no `python` command until the
venv is activated; use `python3`/`python3.11` for that first step.

On Apple Silicon, keep the terminal, Python, and native dependencies on the
same architecture (normally arm64). Do not mix Rosetta/x86_64 packages with an
arm64 environment. Check with `python -c "import platform; print(platform.machine())"`.
If pip falls back to a native source build, install Xcode Command Line Tools
(`xcode-select --install`) and any library named by the build error. Prefer
compatible wheels; do not install every optional dependency to silence warnings.

## 4. Windows (PowerShell)

> **Not verified on Windows** for the `dashboard` extra and the start-up smoke
> (`scripts/smoke_server.py`). The steps below are unchanged; the CI's
> `windows-latest` cell runs the same smoke when CI runs.

Install Python 3.11+ with the Python launcher (`py`) and Git for Windows. Reopen
PowerShell after installation. These examples select Python 3.11 explicitly.

```powershell
py -3.11 --version
git --version

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
# Web applications and verification tools:
python -m pip install -e '.[dev]'
```

### PowerShell execution policy

If script activation is blocked, **activation is optional**. Leave the policy
unchanged and use the interpreter directly for installation and startup:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe dashboard/stira_server.py
```

If your organization's policy permits running reviewed local scripts, inspect
`Get-ExecutionPolicy -List` first. You may use
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` for this
PowerShell session only, then activate again. Do not change machine policy or
try to override Group Policy; use the direct interpreter instead.

### UTF-8 and native dependencies

Japanese Windows locales can default to **cp932**. Before launching Python,
set UTF-8 mode and standard-stream encoding in the same PowerShell session:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

Save documentation/configuration as UTF-8. `chcp 65001` alone does not fix every
Python file/redirected-output encoding. Some symlink tests require Windows
Developer Mode. Native Windows does not provide POSIX `termios`/`fcntl`/PTYs or
`crontab`; use the compatible tools below, or a separate WSL environment with
the Linux instructions (do not reuse a Windows venv inside WSL).

### POSIX-only safeguards on Windows (#540)

Python 3.11 on Windows lacks several POSIX calls. Their behaviour there
(`src/stiracore/_platform.py`):

- Owner-only `0600` on the audit-chain head and the scheduler log head is **not
  applied** (`os.fchmod` is missing; Windows mode bits only mean read-only). Keep the
  data directory inside your user profile so its ACL keeps the files private.
- Restoring a removed memory sets its time without following a link: where
  `os.utime` cannot refuse a link, a symbolic link is refused by `lstat` first.
- The YOLO host **command** channel (Unix socket, peer credentials) is not offered:
  it fails closed, so host-confirmed operations are refused (`host_channel_unavailable`).
  Use the dashboard (browser) confirmation instead.
- `run_checks` and `page_check` refuse to run (`*_platform`); no process groups are killed.

### Optional process-control script

[`scripts/stira_ctl.ps1`](../scripts/stira_ctl.ps1) starts, checks, stops, or
restarts the chat server. **It hard-codes `.venv311\Scripts\python.exe`, not
`.venv`.** To use it, prepare that separate environment once:

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install --upgrade pip
.\.venv311\Scripts\python.exe -m pip install -e '.[dev]'
# Optional Japanese Sokudan classifier, only on Python 3.11:
# .\.venv311\Scripts\python.exe -m pip install -e '.[jev]'

.\scripts\stira_ctl.ps1 start
.\scripts\stira_ctl.ps1 status
# When finished:
.\scripts\stira_ctl.ps1 stop
# To relaunch after configuration changes:
# .\scripts\stira_ctl.ps1 restart
```

Use the script only when local script execution is permitted. Otherwise run the
`.venv` interpreter directly in the foreground and stop it with Ctrl+C.
`start` only requests launch; `status` checks `/api/gateway/status`. Logs are
`.local/stira.out.log` and `.local/stira.err.log`. The script uses port 5555
unless `STIRA_PORT` is set; it does not read the gateway config's port itself.
First classifier initialization may take longer than ordinary startup.

## 5. AWS and environment variables

> AWS is needed only for the **Bedrock** provider (the default). To run without
> AWS, use Ollama, llama.cpp, or an API provider as described in
> [providers.md](providers.md); then this section's `AWS_*` variables do not apply.

Complete [Authentication & Environment](setup.md) before live chat. It is the
single guide for credentials, profiles/SSO, IAM, and model access.

**AWS authentication is required for Bedrock, but exporting every `AWS_*`
variable is not.** Profiles, SSO, or attached roles can supply credentials
without access-key environment variables. Set optional tool variables before
starting the process; the registry is initialized at import time.

| Variable | Required? | Purpose / caveat |
|----------|-----------|-----------------|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Only with environment-key authentication | Credential pair; not needed when using a profile/SSO/role. See setup.md. |
| `AWS_SESSION_TOKEN` | With temporary environment credentials | Leave unset for long-lived keys. |
| `AWS_PROFILE` | When selecting a named profile/SSO profile | Uses the boto3 credential chain. |
| `AWS_REGION` | Optional override; a valid region is required | Read explicitly by `stira_server.py`; otherwise uses gateway configuration (default `ap-northeast-1`). |
| `AWS_DEFAULT_REGION` | Optional SDK/CLI default | Does not override an explicit region or the model factory's default; not the chat server's explicit override. |
| `PERPLEXITY_API_KEY` | Optional | Registers `perplexity_search` when nonempty; sends requests to Perplexity and may incur charges. |
| `STRANDS_KNOWLEDGE_BASE_ID` | Optional | Enables the standard Bedrock Knowledge Base `memory` tool; requires suitable AWS permissions. Not required for local Stira memory. |
| `STIRA_WEB_SEARCH_ENDPOINT` | Optional | Configures generic `web_search`; without it that tool returns a configuration error. Independent of Perplexity. See [endpoint contract](tool-audit.md#web検索endpoint契約). |
| `STIRA_MODEL` | Optional | Model ID override for the chat server (Bedrock model/inference-profile ID by default). |
| `STIRA_PROVIDER`, `STIRA_BASE_URL`, `STIRA_API_KEY_ENV` | Optional | Select a non-Bedrock provider, its endpoint, and the NAME of the variable holding its API key. See [providers.md](providers.md). |
| `STIRA_PORT` | Optional | Chat server / PowerShell helper port (default 5555); not the Flask CLI's port option. |
| `STIRA_STATE_PATH` | Optional | Observation console's state file; does not automatically attach it to the chat server. |
| `PYTHONUTF8`, `PYTHONIOENCODING` | Recommended on Windows | Use `1` and `utf-8` respectively to avoid cp932 failures. |

Never paste real credentials into documentation, shell scripts, or Git commits.

## 6. Tool compatibility by OS

Sources: [tool audit](tool-audit.md), current
[`READ_TOOLS` / `WRITE_TOOLS` registry](../src/stiracore/tools/tool_registry.py),
and upstream `strands-agents-tools` 0.8.9 dependency/source inspection. The audit
is a catalog decision record, **not an OS test matrix**. Entries below describe
expected native-OS support, not successful end-to-end execution on every OS.
Dependency versions can change because installation is not pinned.

- ✅ = registered candidate with no known OS-specific blocker in this inspection;
  still requires successful dependency imports, permissions, and any service setup.
- ⚠️ = conditional dependency, service, or restricted mode; see Notes.
- ❌ = unavailable in this harness/OS combination; see whether the reason is an
  OS limitation or deliberate non-registration.
- READ/WRITE are registry policy classes, not a guarantee of no external traffic.
  New `ToolConfig` defaults to `trust_all`, but the breaker still takes priority.
  Unsupported/missing imports are logged and skipped; inspect your loaded list.

| Tool | Linux | macOS | Windows | Notes |
|------|-------|-------|---------|-------|
| shell | ✅ | ✅ | ❌ | WRITE; POSIX termios/PTY dependency (pty/tty path also relies on fcntl). Not a PowerShell tool. |
| file_read | ✅ | ✅ | ✅ | READ; bounded local UTF-8/BOM text reader. |
| view_image | ✅ | ✅ | ✅ | READ; Pillow-backed PNG/JPEG/GIF/WebP reader with size limits. |
| image_reader | ✅ | ✅ | ✅ | READ; upstream compatibility tool; prefer bounded view_image. |
| current_time | ✅ | ✅ | ✅ | READ; local time lookup. |
| stop | ✅ | ✅ | ✅ | READ; conversation control. |
| handoff_to_user | ✅ | ✅ | ✅ | READ; requires a suitable interactive input channel. |
| file_write | ✅ | ✅ | ✅ | WRITE; filesystem permissions/paths apply. |
| editor | ✅ | ✅ | ✅ | WRITE; filesystem permissions/paths apply. |
| http_request | ✅ | ✅ | ✅ | WRITE; network access required. |
| web_fetch | ✅ | ✅ | ✅ | WRITE; bounded HTTP(S) text retrieval; not browser automation. |
| web_search | ⚠️ | ⚠️ | ⚠️ | WRITE; requires STIRA_WEB_SEARCH_ENDPOINT. |
| perplexity_search | ⚠️ | ⚠️ | ⚠️ | READ in current registry despite external requests; requires PERPLEXITY_API_KEY. |
| memory | ⚠️ | ⚠️ | ⚠️ | WRITE; requires STRANDS_KNOWLEDGE_BASE_ID and Bedrock Knowledge Base access. |
| journal | ✅ | ✅ | ✅ | WRITE; local persistence. |
| cron | ⚠️ | ⚠️ | ❌ | WRITE; system crontab required; native Windows has no crontab. May import even when it cannot run. |
| rss | ⚠️ | ⚠️ | ⚠️ | WRITE; optional feedparser dependency and network access. |
| diagram | ⚠️ | ⚠️ | ⚠️ | WRITE; optional diagrams/graphviz/matplotlib/networkx dependencies; Graphviz dot executable needed for Graphviz output. |
| speak | ⚠️ | ⚠️ | ⚠️ | WRITE; fast playback uses macOS say, Polly playback uses macOS afplay. On Linux/Windows use Polly with play_audio=false and AWS Polly access. |
| batch | ✅ | ✅ | ✅ | WRITE; child tools retain their own OS/service requirements. |
| think | ✅ | ✅ | ✅ | WRITE, exec class (hidden unless `STIRA_ENABLE_EXEC_TOOLS=1`); re-runs parent tools in an inner agent without NXP hooks. |
| graph | ✅ | ✅ | ✅ | WRITE; child-agent/model/tool setup required. |
| use_agent | ✅ | ✅ | ✅ | WRITE; child-agent/model/tool setup required. |
| swarm | ✅ | ✅ | ✅ | WRITE; child-agent/model/tool setup required. |
| browser | ❌ | ❌ | ❌ | Not registered on any OS. Upstream browser backends require separate browser/session setup. |
| use_computer | ❌ | ❌ | ❌ | Not registered; optional GUI/OpenCV (cv2) dependencies are not installed by the base package. |

`browser` is **not simply a Windows cv2 failure**: the audited 0.8.9 package
places OpenCV in the `use-computer` extra, while browser backends use Playwright.
Neither tool is in this harness's allowlist, so adding cv2 alone will not enable
them. Other audit entries marked deferred/unnecessary are also not registered
on any OS; `trust_all` does not auto-import the full 49-module catalog.

Local agent-scoped memory tools (`memory_read`, `memory_search`, `memory_list`,
`memory_write`) are created with `create_memory_tools(store)` and are wired separately by the
chat server. They use local Markdown/SQLite, not the AWS Knowledge Base.
MCP tools depend on the configured server and are not enabled by installation.

For optional RSS/diagram support, install only what you need:

```bash
python -m pip install 'strands-agents-tools[rss,diagram]'
# Graphviz renderer, if needed (choose your OS):
# Ubuntu/Debian: sudo apt install graphviz
# macOS: brew install graphviz
# Windows: install Graphviz and add its bin directory to PATH
```

Check loaded tool names without invoking them (warnings identify skipped tools):

```bash
python -c "from stiracore.tools.tool_registry import READ_TOOLS, WRITE_TOOLS; print('READ:', sorted(READ_TOOLS)); print('WRITE:', sorted(WRITE_TOOLS))"
```

## 7. Start a web application

Activate your venv, install `.[dashboard]` (or `.[dev]`), and run from the
repository root. **Choose one application at a time on port 5555.** These are separate Flask applications,
not two URLs into the same agent. Bind to loopback; neither development server
should be exposed publicly without a separate authentication/deployment design.

### Observation console (no live chat agent)

```bash
python -m flask --app dashboard/app.py run --host 127.0.0.1 --port 5555
```

Open <http://127.0.0.1:5555>. This app provides state/stimulus inspection and
settings; by default it **does not instantiate a Bedrock agent or provide live
chat**. It uses its own state file (override with `STIRA_STATE_PATH`), not an
automatic connection to the chat server. The bare command
`python -m flask --app dashboard/app.py run` uses Flask's default port **5000**;
include `--port 5555` for the URL in this guide. Alternatively,
`python -m dashboard.app` explicitly binds to 127.0.0.1:5555.

### Chat UI (live Bedrock agent)

After completing [AWS setup](setup.md):

```bash
python dashboard/stira_server.py
```

Open <http://127.0.0.1:5555/chat> (the root URL redirects there). This application
creates an `NXPAgent`, loads the sample SOUL (Resolver), and persists local memory
and state under `.local/stira`. Chat messages invoke Bedrock and can execute
loaded tools; they may incur model/tool service charges. Its port follows
`STIRA_PORT`, then gateway configuration, defaulting to 5555. It does not expose
all of the observation console's settings routes.

The server starts and answers HTTP without AWS credentials; only a chat message
contacts the model. If it cannot listen on the port (taken, also when taken while it was starting), it stops before serving with
`Stira: port 5555 is already in use …` (exit code 4) and names
`STIRA_PORT`. A second server of the same checkout is refused first by the
data-directory lock (exit code 3), whatever its port.

Stop a foreground server with Ctrl+C. For the Windows background helper use
`stira_ctl.ps1 stop`; do not start a second server on the same port.

## 8. Health checks and troubleshooting

1. Open <http://127.0.0.1:5555> and confirm the expected app loads. There is no
   shared `/health` route; use the app-specific endpoints below.
2. Observation console: GET `/api/state` should return JSON with HTTP 200.
3. Chat server: GET `/api/gateway/status` should return JSON with HTTP 200.
   This checks application readiness, **not** successful Bedrock invocation;
   after verifying credentials, send a short chat message to test the model.

```bash
# Linux / macOS: select the endpoint for the app you started
curl --fail http://127.0.0.1:5555/api/state
# Chat server instead:
curl --fail http://127.0.0.1:5555/api/gateway/status
```

```powershell
# Windows: select the endpoint for the app you started
Invoke-RestMethod http://127.0.0.1:5555/api/state
# Chat server instead:
Invoke-RestMethod http://127.0.0.1:5555/api/gateway/status
```

To check that a fresh checkout starts, without choosing a port or calling a model:

```bash
python scripts/smoke_server.py   # starts the chat server on an unused port,
                                 # GET /api/config and /api/gateway/status = 200, stops it
```

It prints `smoke: PASS` and exits 0; otherwise the last 20 lines of the server's
stderr (masked) and exit 1. PASS also needs the started process to be identified
(it reports a workspace inside its own, randomly named temporary directory) and to
stop on SIGTERM by itself, without a forced kill. The data directory, logs, `HOME`
and temp directory are one temporary directory, removed afterwards; the checkout's
`.local/` is not used, so it may run beside a running server. The server sees only
an allow-list of environment variables (no credentials, `AWS_*` or proxy settings
from your shell); requests go to 127.0.0.1 only, without proxies or redirects.

CI runs the same command in its `smoke` job on ubuntu, macOS and Windows: a
read-only token (`permissions: contents: read`), `persist-credentials: false`, no
secrets, triggered by `push`/`pull_request` (not `pull_request_target`).
Remaining: the actions are pinned by tag, like the test job; pin them by commit SHA
once the repository is public.

- **No module named flask:** install `.[dashboard]` (or `.[dev]`) with the same
  Python used to start.
- **Address already in use / `Stira: port … is already in use`:** stop your other
  app, or choose another port (`--port` for Flask CLI, `STIRA_PORT` for chat) and
  update the check URL. `lsof -nP -iTCP:5555 -sTCP:LISTEN` shows the owner on
  macOS/Linux; `scripts/stira_ctl.sh start` shows it and refuses to start.
- **Credentials/model errors:** follow [setup.md](setup.md); HTTP readiness does
  not prove IAM/model access. Do not print secrets when troubleshooting.
- **Skipping strands_tools…:** consult the compatibility table and optional
  dependency warnings. A registered tool can still fail at runtime when an
  executable or external service is missing.
- **Windows encoding errors:** set the two UTF-8 variables before startup; stop
  and restart an already-running process to inherit changed environment values.

### Chat errors when the model cannot be reached

When a model call fails, the chat, Stage and the turn record (`turn.completed` →
`failure`, plus `failure_http_status` / `failure_provider` / `failure_host`) name the kind.
Only the code, status, provider and host are shown — never the provider's error text or
the request. Other failures keep the generic “Response processing failed”.

| Code (`failure`) | Message | Next step |
|---|---|---|
| `refused` | Cannot connect to the model server (connection refused) | Check it runs and the endpoint |
| `dns_failed` | The model endpoint's host name does not resolve | Check the endpoint in the settings |
| `unreachable` | The model endpoint cannot be reached | Check the network and the endpoint |
| `timeout` | The model did not answer in time | Check the server's load and the endpoint |
| `auth_failed` | The model rejected the credentials (401/403) | Check the API key or permissions |
| `model_not_found` | The model was not found (404) | Check the model name in the settings |
| `throttled` | Rate limited (429) | Wait a moment and send again |
| `server_error` | The model's server failed (5xx) | Try again later |
| `missing_credential` | No credentials for the model | Set the API key variable or the AWS credentials |
| `provider_unavailable` | This provider's extra package is not installed | Install it and restart |
| `bad_request` | The model refused the request (4xx) | Check the model name and settings |
| `context_overflow` | The conversation exceeds the model's input limit | Start a new conversation |
| `ends_with_assistant` | The model refused a conversation that ends with its own reply — one known path is a tool call that comes back as plain text, so no tool result follows (Bedrock: no assistant prefill) | Send again or choose another model (#256) |
| `context_window_too_small` | Not sent: SOUL, Identity Anchors and the tool definitions alone exceed 90% of the known context window (a server could silently drop the SOUL) | Increase the window (`context_window_tokens` / the server), shorten the SOUL, use a model with a larger window, or try `context_profile=small`/`tiny` when the tool definitions are large ([token-estimate.md](token-estimate.md)) |

With llama.cpp, a 503 (model still loading / busy) is reported as `throttled`.

## 9. Development verification

Install `.[dev]` first. From an activated venv:

```bash
python -m pytest tests/ -x
ruff check .
ruff format --check .
```

Without activation on Linux/macOS, use `.venv/bin/python -m pytest tests/ -x`,
`.venv/bin/ruff check .`, and `.venv/bin/ruff format --check .`. On Windows,
use `.\.venv\Scripts\python.exe -m pytest tests/ -x` and
`.\.venv\Scripts\ruff.exe` for the two Ruff commands (substitute `.venv311`
if using the process-control environment). Local test success does not certify
every optional tool or external service on every OS.

## 10. macOS (Apple Silicon) quick setup with the chat server

Use a Python 3.11 venv (the Sokudan `jev` extra requires 3.11). The `dashboard`
extra provides Flask, so `dev` is not needed just to run the chat server:

```bash
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[jev,dashboard]"
```

With the python.org installer, use `python3.11 -m venv .venv` for the first line.
Sokudan does not need torch. The server itself answers HTTP within a few seconds;
the Sokudan model (about 1.2 GB, kept in the Hugging Face cache) is downloaded on
the **first chat turn**, which then takes about 30 seconds or more depending on the
network. After that, inference runs on CPU in about 20 ms.

Start and control the chat server with the bash helper:

```bash
scripts/stira_ctl.sh start
scripts/stira_ctl.sh status
scripts/stira_ctl.sh stop      # also: restart, distill
```

The helper uses `${STIRA_PYTHON:-.venv/bin/python}` (falling back to `python3`)
and records the PID in `.local/stira/stira_server.pid`. `stop` only stops
that PID, after verifying its command line runs this checkout's
`dashboard/stira_server.py`. If port 5555 is already in use, `start` shows the
owning process and refuses to start. In that case, choose another port:
`STIRA_PORT=5556 scripts/stira_ctl.sh start`. Use the same `STIRA_PORT` for
`status`.


## 11. Environment comparison: EC2 / Mac / Windows

The same software runs in three typical setups, none of them preferred over the others. A **Mac**
or **Windows** PC is the usual partner-AI case (one person at the machine, local run, the main
path of this guide). A Linux host such as **EC2** is an option when you want it always on and
headless; the EC2 column summarises the reference setup in [deploy-ec2.md](deploy-ec2.md), and
its network and role notes (private access, instance role) apply to that setup only. Statements
marked *unverified* have not been run on that platform.

| | Mac (partner AI) | Windows (partner AI) | EC2 (optional, always-on host) |
|---|---|---|---|
| Start | `scripts/stira_ctl.sh start` (backgrounds with `nohup`, PID file), or launchd (§12) | `scripts\stira_ctl.ps1 start` (needs the separate `.venv311`), or Task Scheduler (§12, unverified) | `systemctl start stiracore` (`Type=simple` unit, [deploy-ec2.md §5](deploy-ec2.md#5-running-as-a-service-systemd)); not `stira_ctl.sh` |
| Stays running | launchd LaunchAgent at login (no auto-restart in §12), otherwise until you stop it or log out | Task Scheduler at logon (unverified) | systemd: `Restart=on-failure`, started at boot |
| Stop | `stira_ctl.sh stop` (SIGTERM) or `launchctl bootout`: graceful | `stira_ctl.ps1 stop`: `taskkill /F`, **graceful stops do not run** ([operations.md §1](operations.md#1-start-stop-status)) | `systemctl stop`: SIGTERM, graceful stops run |
| Who can reach it | `127.0.0.1` in the browser | `127.0.0.1` in the browser | loopback only; reach it by SSM port forward or another tunnel (unverified); never a public address |
| Bedrock credentials | profile / SSO / environment ([setup.md](setup.md)); SSO sessions expire | profile / SSO / environment ([setup.md](setup.md)) | **instance role**, no key on the host ([deploy-ec2.md §4](deploy-ec2.md#4-bedrock-access-the-instance-role)) |
| Where secrets live | `~/.aws` (profile and SSO cache); **never** in a launchd plist | `%USERPROFILE%\.aws`; never in a Task Scheduler action | none needed for AWS; the optional `/etc/stiracore/stira.env` is `root:root 0600` |
| Data directory | `.local/stira` in the checkout | `.local\stira` in the checkout | `.local/stira`, on its own encrypted volume mounted at that path (a symlink makes the server stop) |
| Tools ([§6](#6-tool-compatibility-by-os)) | macOS column; `speak` plays through `say` / `afplay` | Windows column: no `shell`, no `cron` | Linux column; exec-class tools stay hidden |
| Paths, encoding, time | POSIX paths; UTF-8 by default | backslash paths; **cp932 by default**: set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` ([§4](#4-windows-powershell)) | POSIX paths; set `PYTHONUTF8=1` (the unit does); UTC unless you set the zone |
| Backup | stop, then copy `.local/stira` ([operations.md §8](operations.md#8-backup-and-restore)); a backup tool that copies while the server runs is not that | same as Mac | stop, then snapshot or copy the data volume ([deploy-ec2.md §6](deploy-ec2.md#6-data-backup-and-restore)) |
| Restore | rules come back old: **lock them** first ([operations.md §8](operations.md#8-backup-and-restore), #273) | same | same |
| Update | stop → fetch → `pip install -e` → `smoke_server.py` → `stira_ctl.sh restart` | same, with `stira_ctl.ps1 restart` | same, with `systemctl start` |
| Verified here | development platform; launchd mechanics only (§12) | **not verified** ([§4](#4-windows-powershell)) | unit, stop/restart, sandbox, data mount, restore lock: Linux VM (systemd 255); **AWS parts unverified** |
| Not to enable (any column) | `STIRA_ALLOW_REMOTE`, `STIRA_ENABLE_EXEC_TOOLS` | same | same |

## 12. Keep it running: launchd and Task Scheduler

Short examples for the partner-AI case: start the chat server at login, stop it cleanly.
They run the interpreter in the foreground under the scheduler (no PID file, no
`stira_ctl`). Keep secrets out of the files below; both are readable by your account.

### macOS (launchd LaunchAgent)

Save as `~/Library/LaunchAgents/com.example.stiracore.plist`, replacing
`/Users/YOU/StiraCore` with the absolute path of your checkout (launchd expands neither
`~` nor variables) and using the venv from §10:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.stiracore</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/StiraCore/.venv/bin/python</string>
        <string>/Users/YOU/StiraCore/dashboard/stira_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/StiraCore</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUTF8</key>
        <string>1</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <!-- A profile NAME is not a secret; add AWS_PROFILE here if you use one. -->
    </dict>
    <!-- Start at login. No KeepAlive: a crash or a refused start is read from the log, not retried. -->
    <key>RunAtLoad</key>
    <true/>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/StiraCore/.local/stiracore.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/StiraCore/.local/stiracore.launchd.err.log</string>
</dict>
</plist>
```

```bash
mkdir -p -m 700 /Users/YOU/StiraCore/.local        # launchd does not create the log directory
plutil -lint ~/Library/LaunchAgents/com.example.stiracore.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.stiracore.plist   # starts it (RunAtLoad)
launchctl print gui/$(id -u)/com.example.stiracore | grep -E "state|last exit"
curl --fail http://127.0.0.1:5555/api/gateway/status
launchctl bootout gui/$(id -u)/com.example.stiracore   # stop: SIGTERM, then the graceful stops
```

Delete the file to stop it from starting at the next login. Do not run `stira_ctl.sh start`
while the job is loaded (the second server is refused by the data-directory lock). A launchd
job does not see your shell's environment: put what the server needs (for example
`AWS_PROFILE`) in `EnvironmentVariables`, and re-run your SSO login when the session expires.

Measured on macOS (arm64): `plutil -lint` passes, and with a variant that sets
`STIRA_HOST=0.0.0.0` (so the server refuses to start and binds nothing) `launchctl bootstrap`
ran the job at load with the right working directory and environment, the refusal message
reached the `StandardErrorPath` file and `last exit code = 1`; `bootout` removed it. **Not run:**
a full server under launchd (the stop path itself was measured on Linux with systemd).

### Windows (Task Scheduler) — unverified

Nothing below was run on Windows. `stira_ctl.ps1` does not set the UTF-8 variables
itself (§4), so start it from a small wrapper, for example `C:\Users\YOU\bin\start-stira.ps1`:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& "C:\Users\YOU\StiraCore\scripts\stira_ctl.ps1" start
```

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\YOU\bin\start-stira.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "StiraCore" -Action $action -Trigger $trigger `
  -Description "Start the StiraCore chat server at logon"
```

`-ExecutionPolicy Bypass` applies to that one process only; if your organization's policy forbids
it, do not override it: use a `.cmd` wrapper that sets the two variables and runs
`.venv311\Scripts\python.exe dashboard\stira_server.py` instead (§4). Stop with
`.\scripts\stira_ctl.ps1 stop` (this is `taskkill /F`: the graceful stops do not run, the
next start recovers); remove the task with
`Unregister-ScheduledTask -TaskName "StiraCore" -Confirm:$false`.
