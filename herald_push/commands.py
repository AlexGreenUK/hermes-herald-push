"""``/herald-push``: how Herald registers a phone, over the dashboard's authenticated socket
(``command.dispatch``). Answers are JSON for Herald to read.

Slash commands can also be typed on a messaging platform (Telegram, Discord, ...); there they're refused,
or anyone allowed to chat with a bot could register their own phone and receive every bot's messages.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Callable

from . import push
from .store import InvalidRegistration, Device, Store

log = logging.getLogger(__name__)

MAX_ARG = 8192
USAGE = "usage: /herald-push info | register <json> | unregister <device_id> | test <device_id>"


def trusted_surface() -> bool:
    """Only the dashboard socket (Herald, Desktop) may manage phones; fail closed when unsure."""
    if "tui_gateway.server" not in sys.modules and "tui_gateway" not in sys.modules:
        return False
    try:
        from gateway.session_context import session_is_messaging_surface
    except ImportError:
        return False
    try:
        return not session_is_messaging_surface()
    except Exception:
        return False


def make_handler(store: Store, version: str, watching: Callable[[], bool],
                 trusted: Callable[[], bool] = trusted_surface) -> Callable[[str], str]:
    def reply(**data) -> str:
        return json.dumps(data, separators=(",", ":"))

    def handle(raw: str) -> str:
        # Exceptions would be swallowed into a generic "unknown command": always answer.
        try:
            if not trusted():
                return reply(ok=False, error="Herald push can only be managed from the Herald app or the dashboard.")
            raw = (raw or "").strip()
            if len(raw) > MAX_ARG:
                return reply(ok=False, error="too long")
            op, _, rest = raw.partition(" ")
            rest = rest.strip()
            if op in ("", "info"):
                return reply(ok=True, version=version, gateway=store.public_identity(), watching=watching(),
                             devices=len(store.devices()))
            if op == "register":
                try:
                    device = Device.parse(json.loads(rest))
                    store.upsert(device)
                except ValueError as exc:  # InvalidRegistration and JSON errors
                    return reply(ok=False, error=str(exc) if isinstance(exc, InvalidRegistration) else "not JSON")
                log.info("herald-push: registered %s", device.name)
                return reply(ok=True, version=version, gateway=store.public_identity(), watching=watching())
            if op == "unregister":
                return reply(ok=store.remove(rest))
            if op == "test":
                device = store.get(rest)
                if device is None:
                    return reply(ok=False, error="not registered")
                _, _, sig = store.identity()
                return reply(ok=push.publish(device, push.message("ping"), sig))
            return reply(ok=False, error=USAGE)
        except Exception as exc:
            log.exception("herald-push: command failed")
            return reply(ok=False, error=type(exc).__name__)

    return handle
