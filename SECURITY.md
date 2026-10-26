# Security policy

English | [日本語](SECURITY_ja.md)

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**: the repository's **Security** tab →
**Report a vulnerability**. Please do not open a public issue, discussion or pull request
for a vulnerability. Include what you can of:

- the version or commit you tested;
- steps to reproduce (a minimal configuration and the prompts or requests used);
- the impact: which boundary is bypassed and what an attacker gains;
- the environment (OS, Python version, model provider, chat server settings that matter).

Do not paste real secrets, keys or personal data; use obvious dummies (`sk-test-…`).
We ask for coordinated disclosure: please wait for a fix or an agreed date before publishing.

## Supported versions

StiraCore is **pre-alpha**. Only the latest `main` is supported; fixes are not backported.
Handling is best effort, with no response or fix-time commitment.

## In scope

A way to bypass a boundary the harness says it enforces, for example: secret masking,
the secret-path floor and `protected_dirs`, IdentityGuard, tool consent and approvals
(Always / Ask / Never, approval binding), the YOLO window limits, the circuit breaker,
the SSRF / outbound-request restrictions, and the local web UI's Host / Origin / CSRF
checks and API permissions. See the [security model](README.md#security-model),
[tool execution paths](docs/tool-execution-paths.md),
[API permissions](docs/admin-api-permissions.md) and [containment](docs/containment.md).

## Out of scope / known limits

- The harness is **not an OS sandbox** (the [security model](README.md#security-model)
  lists the measured limits; [containment](docs/containment.md) phase 0 observes only).
- An attacker who already runs arbitrary code as the same OS user, or a compromised OS.
- The quality or truthfulness of model output by itself, without a boundary bypass.
