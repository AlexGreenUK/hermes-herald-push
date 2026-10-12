"""``/herald-push``: how Herald registers a phone, over the dashboard's authenticated socket
(``command.dispatch``). Answers are JSON for Herald to read.

Slash commands can also be typed on a messaging platform (Telegram, Discord, ...); there they're refused,
or anyone allowed to chat with a bot could register their own phone and receive every bot's messages.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Callable, Optional

from . import push
from .phone import TOOLS, Phones
from .store import InvalidRegistration, Device, Store

log = logging.getLogger(__name__)

MAX_ARG = 8192
# A phone tool's answer (a calendar, a contact list) is bigger than anything else sent here.
MAX_ANSWER_ARG = 64000
USAGE = ("usage: /herald-push info | register <json> | unregister <device_id> | test <device_id> | "
         "phone tools <json> | phone pending <json> | phone answer <json>")


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


def phone_command(phones: Phones, raw: str, reply: Callable[..., str]) -> str:
    """``phone tools <json>``: what this phone has switched on. ``phone pending <json>``: the tool calls
    waiting for it. ``phone answer <json>``: the result of one. Each carries the phone's ``device_id`` and
    secret ``key``."""
    op, _, rest = raw.partition(" ")
    try:
        data = json.loads(rest.strip())
    except ValueError:
        return reply(ok=False, error="not JSON")
    if not isinstance(data, dict):
        return reply(ok=False, error="not an object")
    device_id, key = data.get("device_id"), data.get("key")
    if not isinstance(device_id, str) or not isinstance(key, str):
        return reply(ok=False, error="bad device_id or key")
    if op == "tools":
        tools = data.get("tools")
        if not isinstance(tools, list):
            return reply(ok=False, error="tools must be a list")
        try:
            return reply(ok=True, tools=phones.report(device_id, key, str(data.get("name") or ""), tools))
        except ValueError as exc:
            return reply(ok=False, error=str(exc))
    if not phones.verify(device_id, key):
        return reply(ok=False, error="unknown phone")
    if op == "pending":
        return reply(ok=True, requests=phones.pending(device_id))
    if op == "answer":
        error = data.get("error")
        if error is not None and not isinstance(error, str):
            return reply(ok=False, error="bad error")
        return reply(ok=phones.answer(device_id, str(data.get("id") or ""), data.get("result"), error))
    return reply(ok=False, error=USAGE)


def make_handler(store: Store, version: str, watching: Callable[[], bool],
                 trusted: Callable[[], bool] = trusted_surface,
                 phones: Optional[Phones] = None) -> Callable[[str], str]:
    def reply(**data) -> str:
        return json.dumps(data, separators=(",", ":"))

    def handle(raw: str) -> str:
        # Exceptions would be swallowed into a generic "unknown command": always answer.
        try:
            if not trusted():
                return reply(ok=False, error="Herald push can only be managed from the Herald app or the dashboard.")
            raw = (raw or "").strip()
            if len(raw) > (MAX_ANSWER_ARG if raw.startswith("phone answer ") else MAX_ARG):
                return reply(ok=False, error="too long")
            op, _, rest = raw.partition(" ")
            rest = rest.strip()
            if op in ("", "info"):
                return reply(ok=True, version=version, gateway=store.public_identity(), watching=watching(),
                             devices=len(store.devices()), phone_tools=sorted(TOOLS))
            if op == "phone" and phones is not None:
                return phone_command(phones, rest, reply)
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
