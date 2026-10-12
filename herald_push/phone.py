"""Phone tools: agent tools that run on a phone with Herald.

The tools are a fixed list, reviewed here. A tool is offered to the agent only while a phone says it has it
switched on (``/herald-push phone tools``); every tool starts off on the phone, and a phone that turns one off
takes it away from the agent on the next turn.

A call is a request file in ``<data>/phone/``. The phone learns about it from the agent's tool event (when
it watches that chat), from a push (when it registered for push), or when it connects, reads it with
``/herald-push phone pending`` and answers with ``/herald-push phone answer``. The tool waits for the answer
file, and after its timeout tells the agent the phone didn't answer. Files, not memory: the agent can run in
another process (turn isolation, cron, the messaging gateway) than the dashboard the phone talks to.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .store import DEVICE_ID_RE, Store, _write_private

log = logging.getLogger(__name__)

TOOLSET = "herald_phone"

# A phone not heard from for this long no longer offers its tools: Herald was removed, or signed out.
STALE_SECONDS = 14 * 24 * 3600
# How often a waiting tool looks for the answer.
POLL_SECONDS = 0.25
# Requests left behind by a process that died are removed after this long.
LEFTOVER_SECONDS = 3600
MAX_RESULT = 60000
MAX_NAME = 64
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# How long the agent waits. Reads answer at once; a change waits for you to allow it on the phone.
READ_TIMEOUT = 30
CONFIRM_TIMEOUT = 120


def _schema(name: str, description: str, properties: dict, required: Optional[List[str]] = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_ISO = "Local date and time, ISO 8601 (2026-10-14T09:30). Without a time, the start of that day."

# name -> (schema, timeout in seconds, emoji)
TOOLS: Dict[str, tuple] = {
    "phone_battery": (_schema(
        "phone_battery",
        "The user's phone battery: level in percent, whether it's charging and from what, and the estimated "
        "time until full when the phone knows it.",
        {}), READ_TIMEOUT, "🔋"),
    "phone_network": (_schema(
        "phone_network",
        "How the user's phone is connected: Wi-Fi, mobile data, ethernet or offline, whether the connection is "
        "metered, and whether a VPN is on.",
        {}), READ_TIMEOUT, "📶"),
    "phone_sound": (_schema(
        "phone_sound",
        "Read or change the user's phone sound: Do Not Disturb, the ringer mode and stream volumes. A change "
        "is shown on the phone and only happens if the user allows it.",
        {
            "action": {"type": "string", "enum": ["get", "set"]},
            "dnd": {"type": "string", "enum": ["off", "priority", "alarms", "silence"],
                    "description": "Do Not Disturb, for action=set."},
            "ringer": {"type": "string", "enum": ["normal", "vibrate", "silent"], "description": "For action=set."},
            "stream": {"type": "string", "enum": ["ring", "notification", "media", "alarm"],
                       "description": "Which volume to set, with volume."},
            "volume": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Percent, with stream."},
        }, ["action"]), CONFIRM_TIMEOUT, "🔔"),
    "phone_alarm": (_schema(
        "phone_alarm",
        "Set an alarm or a timer in the user's phone clock app. Shown on the phone and only set if the user "
        "allows it.",
        {
            "kind": {"type": "string", "enum": ["alarm", "timer"]},
            "hour": {"type": "integer", "minimum": 0, "maximum": 23, "description": "Alarm hour, 24-hour clock."},
            "minute": {"type": "integer", "minimum": 0, "maximum": 59, "description": "Alarm minute."},
            "days": {"type": "array", "items": {"type": "string",
                                                "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                     "description": "Repeat on these days; leave out for a one-time alarm."},
            "seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "description": "Timer length."},
            "label": {"type": "string", "maxLength": 80},
        }, ["kind"]), CONFIRM_TIMEOUT, "⏰"),
    "phone_screen_time": (_schema(
        "phone_screen_time",
        "Which apps the user used on the phone and for how long, today or over the last few days, most used "
        "first.",
        {"days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "1 is today only."}}),
        READ_TIMEOUT, "📱"),
    "phone_open": (_schema(
        "phone_open",
        "Open something on the user's phone: a web link, a place or route in the maps app, an installed app, or "
        "the share sheet with some text. Shown on the phone and only opened if the user allows it.",
        {
            "kind": {"type": "string", "enum": ["url", "map", "app", "share"]},
            "value": {"type": "string", "maxLength": 2000,
                      "description": "The https link, the place or address, the app's name, or the text to share."},
            "navigate": {"type": "boolean", "description": "For kind=map: start directions to the place."},
        }, ["kind", "value"]), CONFIRM_TIMEOUT, "📲"),
    "phone_clipboard": (_schema(
        "phone_clipboard",
        "Put text on the user's phone clipboard, ready to paste. Shown on the phone and only copied if the user "
        "allows it.",
        {"text": {"type": "string", "maxLength": 4000}}, ["text"]), CONFIRM_TIMEOUT, "📋"),
    "phone_location": (_schema(
        "phone_location",
        "Where the user's phone is: latitude, longitude, accuracy in meters, when it was measured, and the "
        "nearest address when the phone can look it up.",
        {"fresh": {"type": "boolean",
                   "description": "Measure now instead of using the last known place (slower)."}}),
        READ_TIMEOUT, "📍"),
    "phone_geofence": (_schema(
        "phone_geofence",
        "Places the phone watches: when the user arrives at or leaves one, the phone sends the given message "
        "to this chat as the user. Add (around a point, or where the phone is now when latitude and longitude "
        "are left out), list, or remove. Adding is shown on the phone and only happens if the user allows it.",
        {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "name": {"type": "string", "maxLength": 60, "description": "What the place is called, e.g. Home."},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
            "trigger": {"type": "string", "enum": ["enter", "exit"]},
            "message": {"type": "string", "maxLength": 500,
                        "description": "What the phone sends to this chat when it fires."},
            "id": {"type": "string", "maxLength": 64, "description": "For action=remove, from action=list."},
        }, ["action"]), CONFIRM_TIMEOUT, "🗺️"),
    "phone_calendar": (_schema(
        "phone_calendar",
        "The user's phone calendar: list events in a time range, add an event, or move one. Adding and moving "
        "are shown on the phone and only happen if the user allows it.",
        {
            "action": {"type": "string", "enum": ["list", "add", "move"]},
            "from": {"type": "string", "description": "For list. " + _ISO},
            "to": {"type": "string", "description": "For list. " + _ISO},
            "title": {"type": "string", "maxLength": 200},
            "start": {"type": "string", "description": _ISO},
            "end": {"type": "string", "description": _ISO},
            "all_day": {"type": "boolean"},
            "location": {"type": "string", "maxLength": 200},
            "notes": {"type": "string", "maxLength": 2000},
            "event_id": {"type": "string", "maxLength": 32, "description": "For action=move, from action=list."},
        }, ["action"]), CONFIRM_TIMEOUT, "📅"),
    "phone_contacts": (_schema(
        "phone_contacts",
        "Look up people in the user's phone contacts by name, number or email: their names, phone numbers and "
        "email addresses.",
        {
            "query": {"type": "string", "maxLength": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }, ["query"]), READ_TIMEOUT, "👤"),
    "phone_health": (_schema(
        "phone_health",
        "The user's health data from the phone's Health Connect: steps, sleep sessions or heart rate over a "
        "time range.",
        {
            "metric": {"type": "string", "enum": ["steps", "sleep", "heart_rate"]},
            "from": {"type": "string", "description": _ISO},
            "to": {"type": "string", "description": _ISO},
        }, ["metric"]), READ_TIMEOUT, "❤️"),
}


class Phones:
    """Which phones offer which tools, and the requests waiting for them, in ``<data>/phone/``."""

    def __init__(self, store: Store, wake: Optional[Callable[[str, str], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.root = store.root / "phone"
        self.wake = wake
        self.clock = clock

    # ── which phones offer what ─────────────────────────────────────────────

    def _phones(self) -> Dict[str, dict]:
        try:
            raw = json.loads((self.root / "phones.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def report(self, device_id: str, name: str, tools: List[str]) -> List[str]:
        """Records what [device_id] has switched on (only tools this plugin knows); returns those."""
        if not DEVICE_ID_RE.match(device_id or ""):
            raise ValueError("bad device_id")
        kept = sorted({t for t in tools if isinstance(t, str) and t in TOOLS})
        clean = "".join(ch for ch in str(name or "") if ch.isprintable())[:MAX_NAME].strip() or "Phone"
        with self.store._lock():
            phones = self._phones()
            if kept:
                phones[device_id] = {"name": clean, "tools": kept, "seen": int(self.clock())}
            else:
                phones.pop(device_id, None)
            _write_private(self.root / "phones.json", phones)
        if not kept:
            # Nothing on any more: refuse what is still waiting for this phone.
            for request in self.pending(device_id):
                self.answer(device_id, request["id"], None, "The user turned this tool off on the phone.")
        return kept

    def phone_for(self, tool: str) -> Optional[str]:
        """The phone that offers [tool], the most recently heard from first."""
        now = self.clock()
        offering = [(v.get("seen", 0), k) for k, v in self._phones().items()
                    if tool in (v.get("tools") or []) and now - int(v.get("seen", 0)) < STALE_SECONDS]
        return max(offering)[1] if offering else None

    def offered(self, tool: str) -> bool:
        return self.phone_for(tool) is not None

    # ── requests ────────────────────────────────────────────────────────────

    def _path(self, request_id: str, kind: str) -> Path:
        return self.root / f"{request_id}.{kind}.json"

    def call(self, tool: str, args: dict, session_id: str = "") -> str:
        """Runs [tool] on a phone and waits for its answer: the tool result the agent reads, as JSON."""
        device_id = self.phone_for(tool)
        if device_id is None:
            return _error("No phone offers this tool any more.")
        timeout = TOOLS[tool][1]
        request_id = uuid.uuid4().hex
        now = self.clock()
        request = {"id": request_id, "device_id": device_id, "tool": tool, "args": args if isinstance(args, dict) else {},
                   "session": str(session_id or ""), "created": int(now), "expires": int(now + timeout)}
        _write_private(self._path(request_id, "req"), request)
        if self.wake is not None:
            # The push goes out beside the wait: ntfy may take seconds, and the phone may already be listening.
            threading.Thread(target=self._wake, args=(device_id, request_id), daemon=True).start()
        try:
            deadline = now + timeout
            while self.clock() < deadline:
                answer = self._read(self._path(request_id, "res"))
                if answer is not None:
                    if answer.get("ok"):
                        return json.dumps(answer.get("result"), ensure_ascii=False)
                    return _error(str(answer.get("error") or "The phone couldn't do it."))
                time.sleep(POLL_SECONDS)
            return _error("The phone didn't answer in time. Herald may be closed, offline or not connected to "
                          "this gateway, or the user didn't respond.")
        finally:
            for kind in ("req", "res"):
                try:
                    self._path(request_id, kind).unlink()
                except OSError:
                    pass

    def _wake(self, device_id: str, request_id: str) -> None:
        try:
            self.wake(device_id, request_id)
        except Exception:
            log.warning("herald-push: couldn't wake the phone for a tool", exc_info=True)

    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def pending(self, device_id: str) -> List[dict]:
        """Requests waiting for [device_id], oldest first."""
        now = self.clock()
        out = []
        for path in sorted(self.root.glob("*.req.json")) if self.root.is_dir() else []:
            request = self._read(path)
            if request is None:
                continue
            if now - int(request.get("created", 0)) > LEFTOVER_SECONDS:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if request.get("device_id") != device_id or now > int(request.get("expires", 0)):
                continue
            if self._path(str(request.get("id")), "res").exists():
                continue
            out.append({k: request.get(k) for k in ("id", "tool", "args", "session", "expires")})
        return sorted(out, key=lambda r: r["expires"])

    def answer(self, device_id: str, request_id: str, result: object, error: Optional[str]) -> bool:
        """Hands the answer to the waiting tool; only for a request made to [device_id]."""
        if not REQUEST_ID_RE.match(request_id or ""):
            return False
        request = self._read(self._path(request_id, "req"))
        if request is None or request.get("device_id") != device_id:
            return False
        if self.clock() > int(request.get("expires", 0)):
            return False
        body = {"ok": error is None, "result": result} if error is None else {"ok": False, "error": str(error)[:500]}
        if len(json.dumps(body, ensure_ascii=False)) > MAX_RESULT:
            body = {"ok": False, "error": "The phone's answer was too long."}
        _write_private(self._path(request_id, "res"), body)
        return True


def _error(text: str) -> str:
    return json.dumps({"error": text})


def register_tools(ctx, phones: Phones) -> None:
    """Every phone tool, each offered only while a phone has it on."""
    try:
        from tools.registry import no_cache_check_fn
    except ImportError:  # older Hermes: the check is cached for 30 s
        def no_cache_check_fn(fn):
            return fn

    for name, (schema, _timeout, emoji) in TOOLS.items():
        def handler(args, session_id: str = "", _name=name, **_kwargs):
            return phones.call(_name, args, session_id)

        def check(_name=name) -> bool:
            return phones.offered(_name)

        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=no_cache_check_fn(check), emoji=emoji)
