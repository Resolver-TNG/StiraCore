# Business integration plumbing

One existing NXPAgent receives events and uses ordinary Strands tools. No new
personas, agents, block graphs, or external orchestration service are introduced.

## Webhook ingress

Both dashboard applications register `WebhookReceiver`, a Flask Blueprint, at
`POST /api/webhooks/<provider>`. The observer accepts its existing `chat_agent`;
without one, requests fail closed with 503. The production gateway reuses its
chat runner (including episodic recording, persistence, and status updates).
Chat and webhooks share a nonblocking turn lock so their turns cannot overlap.

Set secrets in the **server environment**, never in an agent prompt:

- `WEBHOOK_SECRET_GENERIC` / `WEBHOOK_SECRET_GITHUB`: HMAC-SHA256 of the exact
  request bytes; header `X-Hub-Signature-256: sha256=<hex digest>`.
- `WEBHOOK_SECRET_SLACK`: Slack signing secret; `X-Slack-Signature: v0=<hex>`
  over `v0:<X-Slack-Request-Timestamp>:<raw bytes>`. Timestamp tolerance is 300s.

Requests must be JSON objects, with `Content-Type: application/json`, at most
64 KiB. Slack signed `url_verification` challenges are answered without a turn.
Verified payloads are explicitly labeled untrusted data and passed as the normal
NXP prompt. The existing classifier derives stimulus; callers cannot select
NeuralState emotion/intensity. NXP alone performs pre/post-turn processing.
The HTTP response never contains agent output or exception details.

Status: 200 processed/duplicate, 400 invalid JSON, 401 invalid signature, 404
unknown provider, 413 oversized, 503 unconfigured/busy/failed. Busy replies have
`Retry-After: 5`. Exact successfully processed bodies are deduplicated per provider
for five minutes (bounded to 1024 entries); failed turns may retry. A failed turn
can already have performed side effects: this is **not** exactly-once delivery.
Generic/GitHub signatures carry no timestamp, so old signed requests can be
replayed beyond that cache window. Use a trusted ingress adapter for freshness
requirements and application-specific event IDs/idempotency.

Deploy one worker. The lock/cache are process-local, not a durable queue. Slack's
short acknowledgement deadline may be exceeded by synchronous inference; use a
trusted durable ingress queue in front for production Slack throughput. Add
reverse-proxy rate limits and TLS. Existing dashboard/chat authentication must
remain in place; expose only webhook routes with their independent signature
check. Do not let inbound event text become operator authorization for tools.

## Generic OAuth configuration

Configuration belongs to the hosting application, not the LLM tool arguments:

```python
import os
from stiracore.integrations.oauth import (
    GenericOAuthConnector,
    OAuthConfig,
    register_connector,
)

connector = GenericOAuthConnector(
    "generic",
    OAuthConfig(
        authorize_endpoint="https://auth.example/authorize",
        token_endpoint="https://auth.example/token",
        revoke_endpoint="https://auth.example/revoke",
        client_id=os.environ["BUSINESS_CLIENT_ID"],
        client_secret=os.environ.get("BUSINESS_CLIENT_SECRET", ""),
        redirect_uri="https://app.example/oauth/callback",
        scopes=("items.read",),
        api_origins=("https://api.example",),
        allowed_methods=("GET",),
    ),
)
register_connector(connector)
# Send this URL to the operator via an authenticated UI:
url = connector.authorize_url()
# In that same operator's authenticated callback session:
# connector.exchange_code(code=request_code, state=request_state)
```

The host must bind its authenticated callback session to the issued state. No
public OAuth callback/login UI is installed by this library. Each connector
keeps at most 32 pending, ten-minute state/PKCE S256 pairs in memory; callback
state is single-use. Restarting invalidates pending authorizations. Scopes are
requested at authorization; enforce least privilege at the provider too.

Tokens are atomically stored at `.local/stira/oauth/<provider>.json` (ignored
by git; POSIX token files 0600, newly created directory 0700). They are currently
**plaintext**: disk encryption/ACLs, private backups, and trusted local tool access
are required. Encryption/keychain and user/account namespaces are future work.
`refresh()` preserves an omitted refresh token and supports rotation;
`access_token()` refreshes near expiry. `revoke()` revokes refresh/access tokens
and only removes the local file after successful responses. An unconfigured
revocation endpoint raises rather than pretending remote revocation succeeded.
Maintain one connector instance per provider/token file; no cross-process token
locking is provided. The generic adapter uses client credentials in POST form
and Bearer tokens; provider-specific OAuth dialects need separate adapters.

## Unified tool surface and restrictions

`webhook_send(url, payload)` and `oauth_api_call(provider, method, url, body=None)`
are registered WRITE_TOOLS and included in StiraConfig's blocked names. Existing
ToolConfig and NXP turn filtering withhold both on WARNING/CRITICAL restrictions,
including OAuth GET because API calls/refresh may have side effects. Like other
raw tool functions, direct Python calls are **not** an independent policy gate;
invoke through the guarded agent/dispatcher.

HTTP: HTTPS only, no URL credentials, no redirects, no ambient proxy/netrc auth,
5s connect/20s read timeout, 64 KiB request body and 2 MiB response limits. OAuth
validates the exact configured origin (including port) and method **before**
reading or refreshing tokens. GET-only is the configuration default. API output
is untrusted and truncated to 50k characters; known token values are redacted.
Webhook replies expose status only. Failures return generic errors, not URLs or
credential-bearing service messages. Arbitrary public webhook destinations remain an
operator trust decision, consistent with the existing network tools.

### Outbound destination guard (SSRF)

Every outbound request of `webhook_send`, `oauth_api_call` and the OAuth token,
refresh and revoke calls goes through `stiracore.integrations.http.fetch`:

- **Static URL check** (`check_url`, before any DNS lookup): scheme allowlist
  (`https` only by default), no user info (`user@`, `a@b`), no fragment, no
  spaces/control characters/backslashes, ASCII host (IDN as `xn--`), no IPv6 zone
  identifier. A host whose last label is numeric must be a canonical dotted quad:
  `2130706433`, `0x7f000001`, `0177.0.0.1` and `127.1` are refused.
- **One destination decision** (`is_forbidden_address`): only plainly public
  addresses pass. Loopback, link-local (including cloud metadata `169.254.169.254`
  and `fd00:ec2::254`), private, shared `100.64.0.0/10`, multicast, reserved and
  unspecified addresses are refused, also embedded in IPv6 (IPv4-mapped
  `::ffff:127.0.0.1`, 6to4, Teredo, NAT64). IP literals are refused at
  `OAuthConfig` construction for the token/revoke endpoints and API origins.
- **Resolve once, connect to that address.** The host name is resolved once per
  hop; every answer must pass (a mixed public + private answer is refused). The
  connection is pinned to the checked address, with TLS SNI, certificate check and
  `Host` header kept on the name, so a DNS rebinding between check and connect
  has no second lookup to exploit.
- **Redirects** are not followed (`OutboundPolicy.max_redirects = 0`, unchanged).
  Only the `Location` header of a 3xx is read, never its body. Code that opts in
  (at most 5) gets the same URL and address check on every hop, `GET`/`HEAD`
  without a body only, no https → http downgrade, and credentials only for the
  first origin (scheme, host, port): once a hop leaves it, `Authorization`/`Cookie`
  headers, `auth=` and `cookies=` are dropped for the rest of the call.
- **Limits**: 5 s connect / 20 s per read, and a hard 30 s deadline for the whole
  call (name resolution, connect, headers and body of every hop; a blocked socket
  is shut down at the deadline, the TLS socket for HTTPS, so a slow drip or endless
  `100 Continue` cannot extend it), 2 MiB response.
  The `Host` header of an IPv6 literal is bracketed.

There is no setting that admits loopback or private destinations; the defaults are
the strictest values. Not covered: `web_fetch` / `web_search` (ours, HTTP(S),
destinations a person sees under Ask) and the upstream `http_request` (Never on new
installs) do not use this guard yet; whether they should refuse loopback always or
only during a YOLO window is open item (7) of [ADR 0012](adr/0012-yolo-mode.md) (Proposed). A system DNS
lookup that outlives the deadline is abandoned (its thread finishes in the
background); its answer is not used.

This is the #98 foundation. Fine-grained service/action/path permissions and
HITL approval integration, durable webhook queues, encryption, and individual
service adapters remain separate work; method/origin allowlists are not a
replacement for action-level policy.
