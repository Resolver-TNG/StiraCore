# Slack and Discord bots

These optional entry points run independently of the web dashboard. They use
NXPAgent's guarded runner, record successful turns as sanitized episodic memory,
and save NeuralState even after halted/failed turns. Status reads do not advance
turns or breaker cooldown.

## Slack Socket Mode

1. Install: `python -m pip install -e '.[slack]'`.
2. Create a Slack app, enable Socket Mode, and provision an app-level token with
   `connections:write`. Install the app to your workspace with `chat:write`,
   `commands`, and the history scopes matching subscribed conversations
   (`channels:history`, `groups:history`, `im:history`, `mpim:history`).
3. Subscribe to the relevant bot message events (`message.channels`,
   `message.groups`, `message.im`, `message.mpim`). Create the `/stira` command.
   Invite the bot only into channels whose members are authorized to use it.
4. Supply `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` securely in the process environment.
5. Run: `python -m stiracore.integrations.slack_bot`.

Every non-bot text message in subscribed conversations gets a reply in its thread,
followed by stability delta and breaker state. WARNING/CRITICAL activation adds a
channel-level notice. `/stira status` responds ephemerally without a model call.
Bot messages, edit/subtype events and empty text are ignored. Replies use plain-text
Slack blocks so generated text cannot trigger mentions.

## Discord

1. Install: `python -m pip install -e '.[discord]'`.
2. Create a bot in the Discord Developer Portal. Enable **Message Content Intent**
   both there and in the application (the entry point enables the latter).
3. Invite it with View Channels, Send Messages and Embed Links permissions. Restrict
   channel visibility to authorized users; this scaffold has no separate user ACL.
4. Supply `DISCORD_BOT_TOKEN` securely in the process environment.
5. Run: `python -m stiracore.integrations.discord_bot`.

All visible non-bot text messages receive a same-channel reply and a six-dimension
NeuralState embed. `!status` only displays the embed. Breaker levels map to green,
yellow or red, and update the bot's presence (the **most recently active channel**,
not an aggregate across channels). Generated mentions are disabled. Long replies
are split to each platform's limits. Discord model calls run off its event loop.

## Runtime and boundaries

- Missing tokens or missing optional SDKs log a skip message and exit successfully;
  importing these modules does not import the optional SDKs or start a bot.
- Model/region and identity settings use existing GatewayConfig/AgentConfig and
  `STIRA_MODEL` / `AWS_REGION` overrides. Configure `soul_path` for a persona;
  without it the agent is headless.
- Each workspace/guild + channel has a separate agent, conversation history,
  NeuralState and episodic store. All users and Slack threads **within a channel
  share context**; do not mix confidentiality domains in the same channel.
- Disk data lives under `.local/chat/<platform>/<hashed-conversation-id>/` by
  default; override the root with `STIRA_CHAT_DATA_DIR`. Do not share a data root
  between simultaneously running instances of the same bot.
- Agent turns are serialized per channel. Sessions remain in memory for the
  process lifetime; eviction, distributed workers and replay deduplication are
  outside this scaffold. Short-term SDK history resets on restart, while saved
  NeuralState and episodic context are loaded again.
- No shell, filesystem, web or other host tools are exposed to remote users by
  default. The only writes are host-managed state/episodic persistence, consistent
  with the web runner. This is not a remote administration interface.
- Provider failures produce a generic reply; service logs include only the error
  class. No model/provider exception text is sent to the channel.

## Verification

Offline tests cover injected SDK handlers, real NXP lifecycle with a mocked model,
HALT, persistence, isolated stores, reply limits, mention suppression, commands,
state colors, breaker notifications and missing-token CLI startup. Live Slack,
Discord and Bedrock calls require operator-supplied credentials and are not part
of the automated test suite.
