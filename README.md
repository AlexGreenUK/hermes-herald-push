# herald-push

A Hermes plugin that sends your bots' messages to the [Herald](https://github.com/Giton22/herald) Android app
even when the phone can't reach the gateway (off the tailnet, on mobile data). Messages travel through
[ntfy](https://ntfy.sh), are **end-to-end encrypted** to the phone, and are **signed** by the gateway. ntfy
only ever sees opaque envelopes on unguessable topics. Google services are not involved.

## Install

From Herald: **Settings → Notifications anywhere**. Herald installs this plugin on the gateway it's signed in
to and registers the phone, in one step.

Or by hand on the gateway:

```
hermes plugins install https://github.com/Giton22/hermes-herald-push
```

## How it works

- **Registration.** The phone registers over the dashboard's authenticated connection with
  `/herald-push register`. The command refuses to run on messaging platforms, so someone chatting with a bot on
  Telegram can't add their own phone. At registration the phone and the gateway swap public keys, and each pins
  the other's.
- **Watching.** One watcher, in the dashboard or the messaging gateway process, reads every profile's
  `state.db` read-only and pushes each new final reply in a bot's Bot Chat.
- **Crypto.** P-256 ECDH to an ephemeral key, HKDF-SHA256, AES-256-GCM bound to the topic, and ECDSA P-256
  over everything, verified before decrypting. The wire format is in Herald's
  [`docs/push-protocol.md`](https://github.com/Giton22/herald/blob/master/docs/push-protocol.md).
- **Storage.** The gateway identity and the device list are in `<hermes root>/plugin-data/herald-push/`, with
  file mode 0600.

**Not yet:** replying or approving through push. Those still use the direct connection.

## Phone tools (0.3.0)

Agent tools that run on the phone, in the `herald_phone` toolset: `phone_battery`, `phone_network`,
`phone_sound`, `phone_alarm`, `phone_screen_time`, `phone_open`, `phone_clipboard`, `phone_location`,
`phone_geofence`, `phone_calendar`, `phone_contacts` and `phone_health`.

- **Off until the user turns them on.** Each tool has its own switch in Herald (Settings → Phone tools). Herald
  tells the gateway which ones are on with `/herald-push phone tools`, and a tool no phone has on is not
  offered to the agent at all. Hermes fixes a chat's tools when the chat starts, so a switch changes what new
  chats get; a call from an older chat to a tool that's off now is refused, and so is a call still waiting.
- **Not on messaging platforms.** A turn from Telegram, Discord and the like gets a refusal: others may be
  talking to the bot there.
- **One phone, one key.** The first report binds the phone's id to a secret key (the gateway keeps its
  hash), and every later phone command must carry it.
- **The phone you chat from.** Herald tells the gateway which chat connections it sends messages on
  (`/herald-push phone here`), and a call from a turn started there goes to that phone, even when another
  phone has the same tool on. If that phone has the tool off, the call is refused rather than sent to another
  phone. A turn without a chat connection (a side question or background task, which Hermes runs apart from
  the chat) goes to a phone that sent a turn in the last two minutes. Any other turn no phone started (a cron
  job, Desktop, the dashboard) goes to the phone the user chose for that in Herald
  (`/herald-push phone default`), else to the phone used last.
- **Changes ask first.** Setting an alarm or Do Not Disturb, opening something, copying, adding or moving an
  event and adding a watched place are shown on the phone, and only happen if the user allows them there.
- **How a call travels.** The tool writes a request file to `<hermes root>/plugin-data/herald-push/phone/`
  (files, because the agent can run in another process than the dashboard). Herald learns of it from the
  chat's tool event, a push, or when it connects, reads it with `/herald-push phone pending` and answers with
  `/herald-push phone answer`, both over the dashboard's authenticated connection only. Reads wait 30 seconds
  for the phone, changes 2 minutes; then the agent is told the phone didn't answer.

## Tests

```
pip install cryptography httpx pyyaml pytest
pytest
```
