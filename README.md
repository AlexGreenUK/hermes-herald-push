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

**Not in 0.1:** replying or approving through push. Those still use the direct connection.

## Tests

```
pip install cryptography httpx pyyaml pytest
pytest
```
