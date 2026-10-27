# Deploying to an always-on host (EC2 and similar): a reference setup

English | [日本語](deploy-ec2_ja.md)

StiraCore runs on a Mac, on Windows or on a Linux server such as EC2; none of them is
the intended place. **Running it locally on a Mac or Windows PC is the main path** (start with
[install.md](install.md)). This page is for the case where you want the chat server to keep
running on a host of its own, and takes **EC2 as the example**. Nothing here is required to use
Stira, and it does not make EC2 the preferred environment.

Guidelines for keeping the chat server (`dashboard/stira_server.py`) running on an EC2
instance (the systemd, data and backup parts apply to any Linux host). This is a **guide for
one operator and one agent per instance**, not a managed service: the server has no authentication, no TLS and no permission tiers
([admin-api-permissions.md](admin-api-permissions.md)). Everything below keeps the defaults
and opens nothing; where a step would add a permission or a door, it says so.

The same software runs on a Mac or a Windows PC as a personal partner AI, side by side with
EC2; the differences (start-up, data, credentials, backup, update) are in the table in
[install.md §11](install.md#11-environment-comparison-ec2--mac--windows). Settings and the
data-directory layout: [configuration.md](configuration.md). Recovery and restore:
[operations.md](operations.md).

## What was verified, and what was not

No AWS resource was created or touched for this guide. The systemd behaviour was measured on a
**Linux VM with systemd 255 (Ubuntu 24.04, aarch64, Python 3.12.3) on a Mac**, with the
repository at the commit this page was written against. Anything that needs AWS itself is
marked **unverified**: it is taken from the AWS documentation or from how the pieces are
designed, and has not been run. Do not read "unverified" as "works"; try it on a scratch
instance first. The full record is in [Verification record](#verification-record).

## 1. Defaults that stay as they are

An always-on host raises the stakes of every door that is open. Keep all of these at their
defaults; none of them is needed for a working instance.

| Setting | Default | On EC2 |
|---|---|---|
| `STIRA_HOST` | `127.0.0.1` | Leave unset. The server then listens on loopback only. |
| `STIRA_ALLOW_REMOTE` | refused | Do not set (see [§3](#3-reaching-the-server-without-opening-it)). |
| `STIRA_ENABLE_EXEC_TOOLS` | hidden | Do not set. Exec-class tools (`shell`, `cron`, `use_agent`, `swarm`, `graph`, `think`, `batch`) stay hidden; there is no sandbox (#167). See [§9](#9-execution-tools-acp-and-autonomy-stay-off). |
| `STIRA_SCHEDULER`, `STIRA_HEARTBEAT` | off | Leave off unless you decide otherwise; neither calls a model by itself. |
| `distiller_mode` | `"rule"` | Keep (no model call). `"llm"` spends tokens. |
| `harvest_stage_auto` | `true` | Keep (a person reviews what Harvest stages). |
| Chat-bot / webhook secrets (`SLACK_*`, `DISCORD_BOT_TOKEN`, `WEBHOOK_SECRET_*`) | not set | Do not set; they are extra inbound or outbound paths. |

`GET /api/tools/list` reports `exec_tools_enabled` (measured: `false` on the service below).

## 2. Layout used in this guide

| Item | Value (replace to taste) |
|---|---|
| Service user | `stira` (system user, no login shell, owns everything below) |
| Checkout and venv | `/opt/stira/StiraCore` (`.venv` inside it) |
| Data directory | `/opt/stira/StiraCore/.local/stira` (**fixed by the code**: `<repository>/.local/stira`; there is no environment variable to move it) |
| Service environment file | `/etc/stiracore/stira.env` (`root:root`, mode `0600`) |
| Port | `5555` on `127.0.0.1` (`STIRA_PORT` changes it) |

Python 3.11 or newer ([install.md §1](install.md#1-prerequisites)); the VM above used 3.12.
Ubuntu's 3.12 and Debian's 3.11 meet it; for Amazon Linux check the Python package offered
by that release (**unverified**). Rebuild `.venv` after an OS upgrade that changes the Python
minor version.

```bash
sudo useradd --system --create-home --home-dir /var/lib/stira --shell /usr/sbin/nologin stira
sudo mkdir -p /opt/stira && sudo chown stira:stira /opt/stira
# Put the checkout in /opt/stira/StiraCore (see §8 for how to get it onto the host), then:
sudo -u stira python3 -m venv /opt/stira/StiraCore/.venv
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install --upgrade pip
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install -e '/opt/stira/StiraCore[dashboard]'
sudo install -d -o stira -g stira -m 0700 /opt/stira/StiraCore/.local /opt/stira/StiraCore/.local/stira
```

**Decide where the data lives before the first start.** If it goes on its own volume, prepare and
mount that volume now ([§6, "A separate volume"](#a-separate-volume-for-the-data-recommended)),
then start the service in §5. Mounting a volume over the data directory of a server that is
already running is not safe; measured on the VM: the files the server had created (23) vanish
from view, it keeps its lock and databases open on the old file system, and a second server
started on the new, empty directory ran at the same time (the data-directory lock no longer
excluded it). An existing installation is moved with the steps in §6.

**A symbolic link at or below the memory directory (`.local/stira/memory`) stops the
server** at start-up with `MemorySymlinkError: Memory paths must not contain symbolic
links.` and a traceback. The directories above it are the operator's choice and are
resolved once at start (#296); before that change a symlinked `.local/stira` stopped the
server too (measured on this guide's instance; the new behaviour was not re-measured on
EC2). To put the data on its own volume, mount it at that path
([§6](#6-data-backup-and-restore)).

## 3. Reaching the server without opening it

The server has **no user authentication and no TLS**. Anyone who can reach the port can read
the conversation, change the settings, **approve tool calls** and send chat messages that spend
model tokens. So the instance must not accept connections from outside, and nothing on it
may listen on a public address.

### Network baseline (all of this is **unverified**: no AWS resource was created)

- **Security group:** no inbound rules at all. Management goes through SSM Session Manager
  (outbound HTTPS from the instance), so port 22 is not needed.
- **No public IP** on the instance (private subnet), or at least no route from the Internet to
  it. A security group with no inbound rule is the second line, not the first.
- **IMDSv2 required** (every metadata request needs a session token; a bare `GET` no longer
  returns the role credentials) and hop limit 1 (the token response does not cross an extra
  network hop, which limits a typical container with its own network namespace; it is not a
  per-process authorisation, and IMDS is still plain HTTP on a link-local address):
  `aws ec2 modify-instance-metadata-options --instance-id i-0123456789abcdef0 --http-tokens required --http-put-response-hop-limit 1 --http-endpoint enabled`
  (dummy ID). Check from the host: an unauthenticated
  `curl -s -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/` should
  answer `401`.
- **Outbound:** the server needs HTTPS to the Bedrock runtime endpoint of your region
  (measured from the VM: DNS and TLS to `bedrock-runtime.<region>.amazonaws.com` work inside
  the sandbox of the unit in §5). To narrow egress, use an interface VPC endpoint for
  `bedrock-runtime` and security-group rules for 443 only; whether global inference profiles
  work through a VPC endpoint is **unverified**. Package installation (PyPI) and the SSM agent
  need their own paths (NAT, or VPC endpoints for SSM).
- **Same-host users:** the CSRF token is handed to any client of the loopback port
  (`/api/csrf`), so every local account on the instance can use the API. Use the instance for
  this one purpose, with administrators you trust.

### Ways to reach the page

Options 2–4 are sketches, not tested recipes.

| Option | Inbound port | Who is authenticated | Extra Stira settings | Status |
|---|---|---|---|---|
| **1. SSM port forward** (default) | none | the IAM principal allowed `ssm:StartSession` | none | **unverified**; the Host/Origin side was measured (below) |
| 2. SSH over SSM (`ProxyCommand`) + `ssh -L` | none | SSH key and IAM | none | unverified |
| 3. Tailscale on the instance (`tailscale serve` to `127.0.0.1:5555`) | none (outbound) | tailnet membership and ACLs | the tailnet name in `STIRA_ALLOWED_HOSTS`, its HTTPS origin in `STIRA_ALLOWED_ORIGINS` | unverified; adds a third-party agent with network reach to the host |
| 4. Reverse proxy with its own authentication and TLS | 443 | the proxy | `STIRA_ALLOW_REMOTE=1` is needed only if the proxy is on another machine; same `…_HOSTS` / `…_ORIGINS` | not recommended here; see below |

Option 1 in practice (needs the Session Manager plugin on your machine, the SSM agent on
the instance and the instance role to include `AmazonSSMManagedInstanceCore`; **unverified**):

```bash
aws ssm start-session --target i-0123456789abcdef0 --region ap-northeast-1 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["5555"],"localPortNumber":["5555"]}'
# then open http://127.0.0.1:5555/chat on your machine
```

If local port 5555 is busy (for example a personal Stira runs there), forward to another
local port. Measured on the service below: a request with `Host: 127.0.0.1:8080` or
`localhost:8080` is served (the port is ignored) and a state-changing request with
`Origin: http://127.0.0.1:8080` and the CSRF token succeeds. A **different host name** is
refused: `Host: stira.internal.example` → `403` until it is listed in `STIRA_ALLOWED_HOSTS`
(then `200`, and its `Origin` matches). Other origins, and a missing token, → `403`.

**Do not share the forwarded port.** A forward binds your machine's loopback; do not bind it to
`0.0.0.0` and do not pass it on (another forward, a shared screen, a team proxy). Whoever
reaches it is the operator for all purposes above. Closing the session closes the door.

### Why not `STIRA_ALLOW_REMOTE=1`

`STIRA_ALLOW_REMOTE=1` is the operator's statement that authentication and TLS exist **in
front** of the server; the server cannot check it. Measured on the service with
`STIRA_HOST=0.0.0.0 STIRA_ALLOW_REMOTE=1`: it listens on `0.0.0.0:5555`, logs a WARNING that
there is no built-in authentication or TLS, and, from the machine's own address, a request
with the default `Host` is refused (`403`) but **the same request with a forged
`Host: 127.0.0.1` is served (`200`), the CSRF token is handed out, and a state-changing POST
with it succeeds (`200`)**. The Host / Origin / CSRF checks defend a browser against other
web pages (DNS rebinding, cross-site requests); they are not a login. So on EC2:

- Do not set it, even "just for the VPC" or "just for my IP": a security group is the only
  thing left between the Internet and an unauthenticated approval endpoint.
- If you must expose the server, a reverse proxy that authenticates **before** forwarding and
  terminates TLS is a precondition, with `STIRA_ALLOWED_HOSTS` / `STIRA_ALLOWED_ORIGINS` set to the public
  name ([admin-api-permissions.md](admin-api-permissions.md)). That design is not covered or
  tested here.

A start that is refused because of a non-loopback `STIRA_HOST` exits with status 1 and a
message naming the cause (measured, for `0.0.0.0` and a LAN address, and `STIRA_ALLOW_REMOTE=true`
is refused too: only the exact value `1` counts).

## 4. Bedrock access: the instance role

Use an **instance role** (instance profile). boto3 finds the role credentials by itself
([setup.md](setup.md)); the server starts without any credentials and reports
`credentials.kind: "aws_chain"`, `configured: null` ("the SDK may still find them", measured). Put
no access key in `/etc/stiracore/stira.env`, in the unit, in the checkout or in
`~/.aws`.

1. Create the invocation policy from [bedrock-iam-policy.md](bedrock-iam-policy.md)
   (`bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the inference
   profiles and their backing foundation models, nothing else; replace the account ID
   `123456789012` with yours). Check that the profile IDs in `gateway-config.json` /
   `STIRA_MODEL` are the ones the policy lists.
2. Create a role that EC2 may assume, and attach the policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
     ]
   }
   ```

3. For Option 1 above, also attach the AWS-managed `AmazonSSMManagedInstanceCore` policy.
4. Create an instance profile for the role and associate it with the instance.

Steps 1–4 are **unverified** (AWS administrator tasks, not run). Check from the host with
`aws sts get-caller-identity` (an `assumed-role/…` ARN), and with the one-turn test in
[bedrock-iam-policy.md](bedrock-iam-policy.md#verify-with-non-admin-credentials).

What the role means for security: **every process on the instance can use the role** (through
IMDS), including anything the agent might run if exec tools were enabled. That is one more
reason to keep them off ([§9](#9-execution-tools-acp-and-autonomy-stay-off)). Keep the role
to invocation only; do not add S3, EC2 or IAM permissions to it for convenience (backups are
taken from outside, [§6](#6-data-backup-and-restore)).

## 5. Running as a service (systemd)

`scripts/stira_ctl.sh` is for a person at a terminal: it starts the server with `nohup`, keeps a
PID file and logs to `.local/stira.out.log`. Under systemd, **let systemd run the server in
the foreground** (`Type=simple`) and use `systemctl` / `journalctl` instead. The ctl script
stays useful for its read-only and CLI subcommands, run as the service user.

The unit is also in [`examples/deploy-ec2/stiracore.service`](../examples/deploy-ec2/stiracore.service)
(a test keeps the two identical):

<!-- unit:begin -->
```ini
# StiraCore chat server as a systemd system service (docs/deploy-ec2.md).
# Copy to /etc/systemd/system/stiracore.service and adjust the paths below:
#   /opt/stira/StiraCore = the checkout (also the data directory's parent)
#   User=stira            = a dedicated, non-login user that owns the checkout's .local/
# Then: sudo systemctl daemon-reload && sudo systemctl enable --now stiracore
# The server binds 127.0.0.1 only (STIRA_HOST is not set). Do not add STIRA_ALLOW_REMOTE here.
[Unit]
Description=StiraCore chat server (stira_server)
Documentation=https://github.com/Resolver-TNG/StiraCore/blob/main/docs/deploy-ec2.md
After=network-online.target
Wants=network-online.target
# If the data directory is a mount point of its own volume, never start without it
# (an unmounted volume would otherwise start on an empty directory of the root disk).
RequiresMountsFor=/opt/stira/StiraCore/.local/stira
# Stop retrying after 5 failed starts in 5 minutes.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=stira
Group=stira
# Configuration files are read relative to the working directory (.local/stira/).
WorkingDirectory=/opt/stira/StiraCore
# Optional; root:root 0600. No AWS key belongs in it when an instance role is attached.
EnvironmentFile=-/etc/stiracore/stira.env
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
SyslogIdentifier=stiracore
ExecStart=/opt/stira/StiraCore/.venv/bin/python /opt/stira/StiraCore/dashboard/stira_server.py

# SIGTERM runs the server's bounded stops (<= 9 s in total); then exit status 0.
KillSignal=SIGTERM
TimeoutStopSec=20
# Restart after a crash or a kill, not after a clean stop. Exit status 2 (credentials
# missing), 3 (data directory owned by another server) and 4 (port in use) are
# configuration problems: retrying cannot fix them.
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=2 3 4

# Files created by the server are owner-only.
UMask=0077

# Hardening. The only writable path is the checkout's .local/ (data directory, configuration).
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/stira/StiraCore/.local
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictNamespaces=true
LockPersonality=true
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
```
<!-- unit:end -->

Install it:

```bash
sudo install -d -m 0755 /etc/stiracore
sudo install -m 0600 /opt/stira/StiraCore/examples/deploy-ec2/stira.env.example /etc/stiracore/stira.env
sudo cp /opt/stira/StiraCore/examples/deploy-ec2/stiracore.service /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/stiracore.service
sudo systemctl daemon-reload && sudo systemctl enable --now stiracore
curl -s http://127.0.0.1:5555/api/gateway/status      # breaker, model, credentials hint, ...
journalctl -u stiracore -f
```

### What each part is for (and what was measured)

- **Stop = SIGTERM, and it is graceful (#285).** `systemctl stop` finished at once, the journal
  shows `StiraCore: stopped (approvals: closed)` (the scheduler and heartbeat appear in that
  line only when they are on), the exit status is 0 and `server.lock` is released by the
  kernel. The server's own stops take at most 9 s, so `TimeoutStopSec=20` is a margin, not a
  wait. A turn waiting on an approval is rejected, the request stays pending in the store and
  is offered as restored after the next start (#240).
- **Restart policy.** `kill -9` of the server: systemd restarted it after `RestartSec`
  (`NRestarts=1`, the new process took the data-directory lock). A clean `systemctl stop`
  does not restart (`on-failure`). Exit statuses seen: `3` when a second server (here started
  by hand) found the directory owned (`Stira: another server (pid N) owns the data
  directory …`); `4` when the port was taken (`Stira: port 5555 is already in use …`);
  `2` is a missing API-key variable for an API provider (not for Bedrock). Those three are
  in `RestartPreventExitStatus`, so the unit goes to `failed` once instead of looping. A
  refused `STIRA_HOST` exits with `1`, which is retried: measured 5 restarts about 6 s apart,
  then `Start request repeated too quickly` and the unit stays `failed` (`StartLimitBurst`);
  fix the cause, then `systemctl reset-failed stiracore`.
- **Non-root, one writable path.** With `ProtectSystem=strict` the whole file system is
  read-only for the service except `ReadWritePaths`. Measured from a probe run with the same
  options: writing to `.local/` and the private `/tmp` works; writing to the repository root
  or `/etc` fails with `Read-only file system`; `/home` and the `0600` env file are not
  readable; DNS and HTTPS to the Bedrock endpoint work. `systemd-analyze security` rates
  the unit `2.9 OK` (a measurement of this unit, not a certificate). The process has no
  capabilities (`CapEff` 0) and `NoNewPrivs` is set. **Not set** (not tested with the
  dependencies): `SystemCallFilter=`, `MemoryDenyWriteExecute=`, `IPAddressAllow=`; add them
  one at a time and watch the journal if you want a lower score.
- **Settings are read relative to `WorkingDirectory`.** Keep `WorkingDirectory` at the
  checkout; a different directory makes the server look for `gateway-config.json` etc.
  elsewhere (`config_location.matches_data_dir: false`,
  [configuration.md](configuration.md#where-configuration-comes-from)).
- **`EnvironmentFile`** is read by systemd (as root), so the file can stay `root:root 0600`; the
  service user cannot read it (measured). The `-` makes it optional: nothing in it is required.
- **`PYTHONDONTWRITEBYTECODE=1`** because the checkout is read-only for the service. The
  server's own `Skipping strands_tools.rss …` lines are normal when the optional extras
  are not installed ([install.md §6](install.md#6-tool-compatibility-by-os)).
- **A warning you will see:** `WARNING: This is a development server. Do not use it in a
  production deployment.` The chat server is Flask's built-in server by design; it is meant for one
  operator on loopback, which is exactly what this layout gives it.

### `stira_ctl.sh` next to systemd

Run these **as the service user**, from the checkout: `sudo -u stira scripts/stira_ctl.sh status`
(measured: works as `stira` and as root), `staging list|show|approve|reject`,
`procedural …` and `distill`. The CLIs write the data directory and the owner-lock check requires
the lock file to belong to the user who runs them: measured, `distill --owner-lock` run as
root against the service user's data directory is refused (`E_LOCK_FILE_UNSAFE`, exit 3). `distill --owner-lock .local/stira` while the service is
running is refused (measured: `E_DATA_DIR_OWNED`); stop the service first or use the timer
unit in [§8](#8-operations). Do not use `stira_ctl.sh start|stop|restart` under systemd:
two supervisors would fight (`stop` also needs `lsof`, which minimal images do not have;
without it `stop` refuses by design). Set `STIRA_PORT` for `status` as well if you changed it.

## 6. Data, backup and restore

Everything the server keeps is in `.local/stira/` ([configuration.md §6](configuration.md#6-data-directory-repositorylocalstira--checked)):
memory (Markdown), `neural-state.json`, tool rules, approvals, checkpoints, `turns.db`,
logs of the scheduler, `workspace/`. It is **private conversation data in plain files**
(memory is plain Markdown, the forgetting archive holds full original text); treat the volume
and every snapshot like the original.

### A separate volume for the data (recommended)

Mount an EBS volume at the data-directory path itself, so the data survives replacing the
instance and can be snapshotted on its own. A symlink does not work (§2). **Do this before the
first `systemctl start`** (§2); the next block is for a fresh host. For a host that already runs
with its data on the root disk, use "Moving existing data onto a volume" below.

```bash
# one-time, volume attached as /dev/nvme1n1 (check with lsblk): format, mount by UUID
sudo mkfs.ext4 -L stira-data /dev/nvme1n1
sudo blkid /dev/nvme1n1                        # copy the UUID into /etc/fstab:
#   UUID=<uuid>  /opt/stira/StiraCore/.local/stira  ext4  defaults,noatime,nofail  0  2
sudo mount /opt/stira/StiraCore/.local/stira
sudo chown stira:stira /opt/stira/StiraCore/.local/stira && sudo chmod 0700 /opt/stira/StiraCore/.local/stira
```

Measured with a loop-backed ext4 image standing in for the volume: the mounted file system
root is `root:root` (and holds `lost+found`) until you `chown`/`chmod` it as above; the server
then starts and writes into it. With the `fstab` line present, **`RequiresMountsFor=` in the
unit makes `systemctl start` fail with `A dependency job … failed` when the volume is
missing** (measured), instead of silently starting on an empty directory of the root
disk. Without an `fstab` entry the guard has nothing to wait for. The volume type, size, IOPS and the
device name are your choices (**unverified** on EBS).

#### Moving existing data onto a volume

Stop everything that writes the data directory first (the maintenance window of
[§8](#before-you-stop-the-server-on-purpose)), then:

```bash
D=/opt/stira/StiraCore/.local        # absolute paths: .local is 0700 stira, an administrator cannot cd into it
sudo systemctl stop stiracore                    # after the §8 steps: timer stopped, no run in progress
sudo mv $D/stira $D/stira.old
sudo install -d -o stira -g stira -m 0700 $D/stira    # the mount point
# format the volume and add the /etc/fstab line as in the previous block, then:
sudo systemctl daemon-reload && sudo mount $D/stira
sudo chown stira:stira $D/stira && sudo chmod 0700 $D/stira
# the server starts only if the copy AND the comparison succeeded:
sudo cp -a $D/stira.old/. $D/stira/ \
  && sudo diff -r -x lost+found $D/stira.old $D/stira \
  && sudo systemctl start stiracore
curl -s http://127.0.0.1:5555/api/gateway/status
```

Measured on the VM, run as a normal administrator (a `cd` into `.local` was refused with
`Permission denied`; the absolute-path form worked): files copied, `diff` identical, and a copy that
failed (missing source) stopped before the `start`. With a loop-backed image the earlier run also
started the server on the volume (`findmnt` shows it) and it answered. Keep `stira.old` until you have checked the service
with real use, then delete it; it holds the same private data as the volume.

**Encryption:** use an encrypted EBS volume (and an account default of EBS encryption if your
policy allows it). The files are not encrypted by Stira. Snapshots of an encrypted volume
are encrypted; never share them publicly.

### Backup

Follow [operations.md §8](operations.md#8-backup-and-restore): stop the server so SQLite has
no open WAL, then copy everything including `.head`, `-wal`, `-shm` and `.locked` files.
Volume snapshots work the same way (**unverified** on EBS), and should be started from
outside the instance (your workstation or a lifecycle policy), so the instance role needs no EC2 or
S3 permission. Two places, in this order:

**1. On the instance** (an SSM or console session as an administrator): open the maintenance
window of [§8](#before-you-stop-the-server-on-purpose) and confirm the server is down.

```bash
sudo systemctl stop stira-distill.timer          # harmless if you never enabled it
until systemctl show -p ActiveState --value stira-distill.service | grep -qE '^(inactive|failed)$'; do sleep 5; done
sudo systemctl stop stiracore
systemctl is-active stiracore                # must print: inactive
sudo sync -f /opt/stira/StiraCore/.local/stira && echo synced   # flush the OS cache; continue only if it prints "synced"
```

Stopping the process closes its files, but what it wrote can still sit in the operating system's
cache, and a snapshot contains only what has reached the volume. `sync -f` flushes the file
system that holds the path (the data volume when it is mounted there; the root file system when
the data lives on the root disk) and exits non-zero on failure. Measured on the VM: exit 0 on
success, exit 1 for a bad path; it needs `sudo` because the directory is `0700 stira`. **Do not
request the snapshot unless `synced` was printed.** If you want the file system held still while
the request is made, `sudo fsfreeze -f <mount point>` before and `sudo fsfreeze -u <mount
point>` after step 2 (also when step 2 failed) work on a mounted volume (measured on a
loop-backed mount); with the server down nothing writes meanwhile, so this is optional. Without
these steps, or with the server running, the snapshot is at best crash-consistent, like a
`kill -9` (next paragraph).

**2. On your workstation** (an IAM principal that may call `ec2:CreateSnapshot`; the instance role
must not):

```bash
aws ec2 create-snapshot --volume-id vol-0123456789abcdef0 --description "stira data" \
  --query SnapshotId --output text               # dummy ID; prints snap-… only when the request succeeded
```

**3. Back on the instance, only if step 2 printed a snapshot ID, and only for a stand-alone
backup.** When the backup is the first part of an update, a restore or a data move, do steps 1 and 2
only: the server and the timer stay down until the last step of that procedure. (AWS documents the snapshot as
the volume at the time of the request, so the server may start while it is still `pending`;
**unverified**): `sudo systemctl start stiracore`, then `sudo systemctl start
stira-distill.timer` if you use it. If step 2 failed, fix it and repeat step 2 first; do not
start the server "just in case" if you still need a consistent copy.

A snapshot taken while the server runs is crash-consistent only, like a `kill -9`. The
server recovers from that case by design (approval and log chains record a recovery, the
last transaction may be missing; [operations.md §5–§7](operations.md#5-approvals-across-a-restart)), but a restore
from such a snapshot was **not tested** here.

### Restore, and the known limit from #273

> **Known limit (#273, open).**
> Restoring a consistent older copy brings back **older, possibly looser tool rules** and passes
> every integrity check inside the data directory. Nothing detects it by itself. A snapshot
> restore of the data volume is exactly this case.

What to do, in order ([operations.md §8](operations.md#8-backup-and-restore) is the
authoritative text):

1. Stop the timer and the service as in [§8](#before-you-stop-the-server-on-purpose) and keep
   them stopped until step 3 is done: a maintenance run that fires in between would start the
   server before the restored rules are locked. Do not mount the restored volume at the live path blindly: attach it
   elsewhere (for example read-only at `/mnt/restore`) and copy from it, so that the live
   approval store and operation ledger are not overwritten.
2. Restore the tool-rule files together (`tool-config.json`, `.head`, `.locked`,
   `settings-history.jsonl`), **not** the approval store (move `hitl-approvals.db*` and `.head`
   aside), **not** an older `hitl-operations.db`; memory and `neural-state.json` as needed.
3. **Lock the restored rules, then start:** `cd /opt/stira/StiraCore && sudo -u stira
   .venv/bin/stira tool-rules lock` (measured on the VM: `tool rules locked (restored_backup)`,
   `stira tool-rules status` then exits 1 and shows `LOCKED`). Every tool is Never until a
   person saves the rules on `/settings#sec-tools` with the recovery word `RECOVER`.
4. If the volume itself was lost and only the restored copy exists, the older operation ledger
   is all there is. Its effect (an already-run call can be asked again if a paused batch is
   replayed) is described in [ADR 0006](adr/0006-hitl-persistence.md#addendum-273-a-ledger-restored-from-a-backup);
   not restoring the approval store avoids the replay.

**Optional anchor outside the data volume (off by default).** With
`STIRA_TOOL_RULES_ANCHOR_DIR` the highest rule generation is also kept in a directory the
data-volume backups do not contain, and an older or reset data directory then locks on its
own (`anchor_rollback`). On EC2, `StateDirectory=` gives a directory on the root disk that
stays writable under `ProtectSystem=strict`. A drop-in
(`/etc/systemd/system/stiracore.service.d/anchor.conf`):

```ini
[Service]
StateDirectory=stiracore
StateDirectoryMode=0700
# Add the next line only AFTER `stira tool-rules init-anchor` has created the anchor, see below:
Environment=STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor
```

Order matters (measured): on a first start with the variable set and no anchor, the rules
load **locked** (`anchor_missing`). So:

1. Create the drop-in with `StateDirectory=` only, **`sudo systemctl daemon-reload`**, start the
   service once (the rules are created), stop it.
2. `sudo -u stira env STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor .venv/bin/stira tool-rules init-anchor`
   (`anchor created at generation 1.`).
3. Add the `Environment=` line, **`sudo systemctl daemon-reload`** again, start the service.
4. **Check that it took effect** (do not assume):
   `pid=$(systemctl show -p MainPID --value stiracore); sudo cat "/proc/$pid/environ" | tr '\0' '\n' | grep -Fx STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor`
   must print the variable (the file belongs to the `stira` process, so `sudo` must open it:
   `sudo tr … < /proc/$pid/environ` fails for a normal administrator, measured), and `systemctl show stiracore -p NeedDaemonReload` must print
   `NeedDaemonReload=no`.

systemd does not re-read a drop-in by itself. Measured: with the unit running, adding the drop-in
and `systemctl restart` without `daemon-reload` printed `Warning: The unit file, source
configuration file or drop-ins of stiracore.service changed on disk. Run 'systemctl
daemon-reload'`, and the new server had no `STIRA_TOOL_RULES_ANCHOR_DIR` and no
`/var/lib/stiracore` (the protection silently off). After `daemon-reload` and a restart both
were there. (A stop followed by a start did pick the files up in the same test, because an
inactive unit is loaded again from disk; do not rely on it.) Measured result of wiping the data
directory afterwards: `LOCKED (anchor_rollback)`. The anchor guards against
restoring the **data volume** only; it is rolled back by anything that also restores the root
disk (an AMI, a root-volume snapshot). Details: [operations.md §8](operations.md#8-backup-and-restore).

### Capacity, retention, rotation

- Right after the first start the data directory was 336 KB (measured); growth with real use
  is **not measured**. Watch it with `df -h /opt/stira/StiraCore/.local/stira` and an
  alarm of your own.
- `turns.db`, checkpoints and the logs are **not rotated by default**. `stira memory retention
  --data-dir .local/stira` (run as `stira`) prints a read-only report; limits are opt-in
  (`memory_retention`, nothing is removed without `--apply`, what `--apply` removes is archived;
  [memory-maintenance.md](memory-maintenance.md#retention-delete-requests-and-provenance-294),
  #294 is still open).
- The server's log goes to the journal (`SyslogIdentifier=stiracore`). Secret values are
  masked in it, but the conversation is not fully masked everywhere
  ([operations.md §13](operations.md#13-what-the-logs-and-stores-mask-291)). Cap the
  journal with `SystemMaxUse=` in `/etc/systemd/journald.conf` and decide whether it persists
  across reboots (`Storage=persistent`; the default differs by distribution, check yours).

## 7. Time

systemd timers use the host's local time zone; an EC2 instance is normally UTC, so a
`04:30` timer runs at 04:30 UTC unless you set the zone (`timedatectl set-timezone Asia/Tokyo`).
The scheduler's jobs carry their own `timezone` field ([scheduler.md](scheduler.md)). Keep
the clock synchronised (Amazon Linux and recent Ubuntu AMIs use chrony with the Amazon Time Sync
Service: AWS documentation, **unverified** here); Neural State decay and the approval
expiry both use the clock (decay is path-independent and resyncs when the clock goes
backwards, [README](../README.md)).

## 8. Operations

### Before you stop the server on purpose

Backup, restore, an update and moving the data (§6) all need the server down **and kept down**.
If you enabled the optional maintenance timer (below), it can fire meanwhile; then it runs the
wrapper against the data you are copying or changing and, because it starts the server again,
can bring the server up in the middle of your work (for a restore: before the rules are locked).
So every one of those procedures starts like this, on the instance:

```bash
sudo systemctl stop stira-distill.timer          # harmless if you never enabled it
# let a run that is already in progress finish ("activating" counts as running; is-active --quiet does not)
until systemctl show -p ActiveState --value stira-distill.service | grep -qE '^(inactive|failed)$'; do sleep 5; done
sudo systemctl stop stiracore
systemctl is-active stiracore                # must print: inactive
```

and ends with `sudo systemctl start stiracore` followed by `sudo systemctl start
stira-distill.timer` (only if `systemctl is-enabled stira-distill.timer` says `enabled`). Measured on
the VM: while a run was in progress the unit state was `activating`; the loop waited for it
(3 s), the run restarted the server on its way out, and the following `stop` left the server
and the timer down.

**Update (stop → fetch → test → start).** A model change or a server update is a restart:

```bash
# first: the window above (timer stopped, no run in progress, server stopped), then a backup (§6, steps 1 and 2 only:
# do NOT run its step 3 — the server and the timer stay down until the end of this update)
sudo -u stira git -C /opt/stira/StiraCore fetch --tags origin
sudo -u stira git -C /opt/stira/StiraCore checkout <tag-or-reviewed-commit>
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install -e '/opt/stira/StiraCore[dashboard]'
sudo -u stira /opt/stira/StiraCore/.venv/bin/python /opt/stira/StiraCore/scripts/smoke_server.py   # smoke: PASS
sudo systemctl start stiracore
sudo systemctl start stira-distill.timer         # only if you use it (see above)
```

The smoke test (measured as `stira`, 1.0 s) starts a throw-away server on an unused port in a
temporary directory and stops it; it does not touch the data directory or call a model.
How the checkout gets onto the host is yours to decide: the repository is private, so a
long-lived Git token on the instance is a secret you would have to protect; a read-only
deploy key or an archive you copy in (`git archive <rev> | …| tar -x`, the method used for
the test VM) are alternatives (**unverified** on EC2). The dependencies are not pinned in the
repository ([install.md §6](install.md#6-tool-compatibility-by-os)); record
`python --version` and `pip freeze` after a good deploy so that a rebuild can match.

**Memory maintenance is optional and off.** Pattern B of
[memory-maintenance.md](memory-maintenance.md#pattern-b--an-os-timer-runs-the-cli-rule-distiller)
(an OS timer runs the CLI, rule distiller, no model call) is the usual choice on an always-on host
**if** you want automation; by default nothing is installed and nothing runs
(pattern A: run `distill` and review `staging` yourself, whenever you like). The wrapper must hold the
data directory's lock, so on a host whose server never stops it needs the server down while it runs.
[`examples/deploy-ec2/stira-distill.service`](../examples/deploy-ec2/stira-distill.service) and
`.timer` do that (stop the server, run the wrapper, start the server again, also after a
failure) and are **not enabled** by default. The server is started again **only if it was
running when the run began** (a marker file in `/run` remembers it). Measured on the VM:
`systemd-analyze verify` clean; a run with the server up stopped it, ran the wrapper (exit 0, one
line in `distill-runs.jsonl`) and started it again; a run that failed on a bad setting started
the server again too; a run begun with the server stopped left it stopped. The
`STIRA_DISTILL_MAX_CALLS` default of `0` keeps it model-free. The unit does not know about your
backups, updates and restores: use the window above before them.

**Cost.** No estimate is given here. The cost categories are the instance, the volumes and
snapshots, any VPC endpoints or NAT, and, above all, Bedrock tokens, which depend on how you
use it (see [prompt-cache.md](prompt-cache.md) and [context-management.md](context-management.md)
for the measured levers). Use the AWS pricing calculator with your numbers. The only sizing
fact measured here: an idle server was 136 MB resident on Python 3.12 / aarch64 without the
optional Sokudan classifier (whose model is about 1.2 GB on disk, [install.md §10](install.md#10-macos-apple-silicon-quick-setup-with-the-chat-server)); memory under real use is not measured.

## 9. Execution tools, ACP and autonomy stay off

On EC2 these are **not enabled and not planned to be**:

- **Exec-class tools** (`STIRA_ENABLE_EXEC_TOOLS`). With them on, the agent's shell runs as the
  server's OS user. It could run the approval CLIs (`staging approve`, `procedural_cli
  approve`) as that user or write the data directory, and it would see the instance role's
  credentials. Stira's own approvals are only as strong as the OS user boundary then
  ([tool-execution-paths.md](tool-execution-paths.md), [ADR 0010](adr/0010-tool-policy-gate-stack.md)).
  If a future need arises, the minimum is the rule in the docs: a different OS user for the
  agent than for the person who approves, with no write access to `memory/` outside
  `episodic/`, plus the #167 sandbox which does not exist yet. That is a design decision, not a
  setting to flip.
- **ACP** (client, server, delegation) is not implemented on `main`
  (#210–#212, [README roadmap](../README.md#roadmap-not-implemented)). Nothing to turn on;
  keep it that way until it has its own approval design.
- **Autonomy.** `autonomy_boundary` in `agent-config.json` is stored but **not enforced**
  ([configuration.md](configuration.md)). The scheduler and heartbeat are off, run no model by
  themselves, and the self-scheduling of agents (ADR 0011) is only proposed. If you turn the
  scheduler on, a person creates each job, and after a recovery a person resumes them
  ([operations.md §6](operations.md#6-scheduler)).
- **Adding permissions.** The instance role (§4) is invocation-only. Granting it more, or
  widening `write_roots` / `allow_broad_write_roots`, is out of scope of this guide.

## Verification record

Run on 2026-10-18 (Ubuntu 24.04 aarch64 VM, systemd 255, Python 3.12.3, the service as the
`stira` user; commands in the sections above). **Not** run: anything on AWS.

| Claim | How it was checked | Result |
|---|---|---|
| The unit is syntactically valid | `systemd-analyze verify` | no output, exit 0 |
| Hardening score | `systemd-analyze security stiracore.service` | `2.9 OK` |
| Starts without AWS credentials, binds loopback only | `ss -ltn`, `/api/gateway/status` | `127.0.0.1:5555`, `credentials.configured: null` |
| `systemctl stop` is a graceful SIGTERM stop (#285) | journal and `systemctl show` | `StiraCore: stopped (approvals: closed)`, status 0, immediate |
| Restart after `kill -9` | `kill -9`, `NRestarts` | restarted after `RestartSec`, `NRestarts=1` |
| Second server on the same data directory | second `stira_server.py` | exit 3, owner pid named |
| Port taken | `python -m http.server` on 5555 | exit 4, no restart |
| Non-loopback host without `STIRA_ALLOW_REMOTE=1` | `STIRA_HOST=0.0.0.0`, `192.0.2.10`; `STIRA_ALLOW_REMOTE=true` | exit 1, `Refusing to bind …` (also on the macOS host) |
| Sandbox effect | probe unit with the same options | `.local` writable; repo root and `/etc` read-only; env file and `/home` unreadable; DNS+TLS to Bedrock endpoint work |
| Host / Origin / CSRF through a tunnel | `curl` with set `Host` / `Origin` | see [§3](#ways-to-reach-the-page) |
| `STIRA_ALLOW_REMOTE=1` is not a login | listen on `0.0.0.0`, forged `Host: 127.0.0.1` | 200, token issued, POST 200 |
| Data volume mount point; missing volume blocks start | loop-backed ext4 + `fstab` + `RequiresMountsFor` | works; `A dependency job … failed` when missing |
| Flush before the snapshot; migration and `environ` check as a normal administrator | `sync -f` (exit 0 / 1 for a bad path), `fsfreeze -f/-u`, `cd` into `0700` `.local`, `sudo tr < …/environ` vs `sudo cat` | `sync -f` and the absolute-path forms work; the redirect forms are refused |
| Volume mounted over a running server | start, then `mount` | files hidden, old file system still open, a second server ran: mount before the first start; moving existing data (copy + `diff`) works |
| Anchor drop-in without `daemon-reload` | add drop-in to a running unit, `restart` | warning, variable and state directory absent; with `daemon-reload` both present |
| Symlinked data directory (before #296) | symlink as `.local/stira` | server dies: `MemorySymlinkError` (since #296 only a link at or below `.local/stira/memory`) |
| Restore lock | `stira tool-rules lock` / `status` as `stira` | `locked (restored_backup)`, status exit 1 |
| Rule-rollback anchor with `StateDirectory=` | `init-anchor`, then data directory wiped | `LOCKED (anchor_rollback)`; variable set before the anchor exists → `anchor_missing` |
| `smoke_server.py` as `stira` | run | `smoke: PASS in 1.0s` |
| Optional distill unit | `systemd-analyze verify`, runs with the server up / stopped / failing; waiting for a run in progress | server started again only if it was running; stays stopped otherwise; `activating` state needs the `ActiveState` loop |
| launchd (Mac) | see [install.md §12](install.md#12-keep-it-running-launchd-and-task-scheduler) | `plutil -lint` OK; a refusing variant ran from the plist |
| Anything on AWS (IMDSv2, SSM, VPC endpoints, roles, EBS, snapshots, Time Sync) | — | **unverified** |
| A real server on macOS or Windows under launchd / Task Scheduler | — | not run (macOS: mechanics only); Windows **unverified** |

## Related

#285 (owner lock, SIGTERM),
#294 (retention),
#273 (restore rolls back rules),
#317 (memory-maintenance examples),
#185 (Host / Origin / CSRF),
#167 (exec sandbox),
#254 (prompt cache),
#107 (public repository). This guide
is linked from the [README](../README.md#documentation-index) and the [docs index](README.md).
