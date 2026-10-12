"""Phone tools: agent tools that run on a phone with Herald.

The tools are a fixed list, reviewed here. A tool is offered to the agent only while a phone says it has it
switched on (``/herald-push phone tools``); every tool starts off on the phone. Hermes fixes a chat's tools
when the chat starts, so a switch changes what new chats are offered; a chat that already has a tool the
user since turned off gets a refusal when it calls it.

A call is a request file in ``<data>/phone/``. The phone learns about it from the agent's tool event (when
it watches that chat), from a push (when it registered for push), or when it connects, reads it with
``/herald-push phone pending`` and answers with ``/herald-push phone answer``. The tool waits for the answer
file, and after its timeout tells the agent the phone didn't answer. Files, not memory: the agent can run in
another process (turn isolation, cron, the messaging gateway) than the dashboard the phone talks to.

Each phone holds a secret key; the gateway keeps only its hash, and every phone command must carry the key,
so one dashboard client can't read or answer another phone's calls. Phone tools refuse turns on messaging
platforms: someone in a group chat with the bot must not get the owner's location or contacts.
"""

from __future__ import annotations

import hashlib
import hmac
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
KEY_RE = re.compile(r"^[0-9a-f]{32,128}$")
# How often each process looks whether a phone changed its tools, to refresh its tool list.
WATCH_SECONDS = 2.0

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

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("ascii")).hexdigest()

    def verify(self, device_id: str, key: str) -> bool:
        """Whether [key] is the secret [device_id] reported with."""
        if not DEVICE_ID_RE.match(device_id or "") or not KEY_RE.match(key or ""):
            return False
        known = self._phones().get(device_id, {}).get("key")
        return isinstance(known, str) and hmac.compare_digest(known, self._hash(key))

    def report(self, device_id: str, key: str, name: str, tools: List[str], push_id: Optional[str] = None) -> List[str]:
        """Records what [device_id] has switched on (only tools this plugin knows); returns those. The first
        report binds the phone id to [key]; a later one with another key is refused. [push_id] is the phone's
        push registration, if any, which a call nudges; it only ever receives a nudge, never the call."""
        if not DEVICE_ID_RE.match(device_id or ""):
            raise ValueError("bad device_id")
        if not KEY_RE.match(key or ""):
            raise ValueError("bad key")
        if push_id is not None and not DEVICE_ID_RE.match(push_id):
            raise ValueError("bad push_id")
        kept = sorted({t for t in tools if isinstance(t, str) and t in TOOLS})
        clean = "".join(ch for ch in str(name or "") if ch.isprintable())[:MAX_NAME].strip() or "Phone"
        with self.store._lock():
            phones = self._phones()
            known = phones.get(device_id, {}).get("key")
            if isinstance(known, str) and not hmac.compare_digest(known, self._hash(key)):
                raise ValueError("this phone id is held by another key")
            # Kept with no tools too: the key stays bound to the id.
            phones[device_id] = {"name": clean, "tools": kept, "seen": int(self.clock()), "key": self._hash(key),
                                 "push": push_id}
            _write_private(self.root / "phones.json", phones)
        _refresh_tool_list()
        # What is still waiting for a tool that's off now is refused.
        for request in self.pending(device_id):
            if request["tool"] not in kept:
                self.answer(device_id, request["id"], None, "The user turned this tool off on the phone.")
        return kept

    def phone_for(self, tool: str) -> Optional[str]:
        """The phone that offers [tool], the most recently heard from first."""
        now = self.clock()
        offering = [(int(v.get("seen", 0)), k) for k, v in self._phones().items()
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
        push_id = self._phones().get(device_id, {}).get("push")
        if self.wake is not None and isinstance(push_id, str):
            # The push goes out beside the wait: ntfy may take seconds, and the phone may already be listening.
            threading.Thread(target=self._wake, args=(push_id, request_id), daemon=True).start()
        try:
            deadline = now + timeout
            while self.clock() < deadline:
                answer = self._read(self._path(request_id, "res"))
                if answer is not None:
                    if answer.get("ok"):
                        return json.dumps(answer.get("result"), ensure_ascii=False)
                    return _error(str(answer.get("error") or "The phone couldn't do it."))
                if self.interrupted():
                    return _error("Stopped before the phone answered.")
                time.sleep(POLL_SECONDS)
            return _error("The phone didn't answer in time. Herald may be closed, offline or not connected to "
                          "this gateway, or the user didn't respond.")
        finally:
            for kind in ("req", "res"):
                _unlink(self._path(request_id, kind))

    def _wake(self, push_id: str, request_id: str) -> None:
        try:
            self.wake(push_id, request_id)
        except Exception:
            log.warning("herald-push: couldn't wake the phone for a tool", exc_info=True)

    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _sweep(self) -> None:
        """Removes what a process that died, or an answer that came too late, left behind."""
        cutoff = time.time() - LEFTOVER_SECONDS
        for pattern in ("*.req.json", "*.res.json", ".tmp-*"):
            for path in self.root.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass

    def pending(self, device_id: str) -> List[dict]:
        """Requests waiting for [device_id], oldest first."""
        if not self.root.is_dir():
            return []
        self._sweep()
        now = self.clock()
        out = []
        for path in sorted(self.root.glob("*.req.json")):
            request = self._read(path)
            if request is None:
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
        path = self._path(request_id, "res")
        _write_private(path, body)
        if not self._path(request_id, "req").exists():
            # The tool gave up while this was written: nobody will read it.
            _unlink(path)
            return False
        return True

    @staticmethod
    def interrupted() -> bool:
        """Whether the user stopped the turn this tool runs in."""
        try:
            from tools.interrupt import is_interrupted
        except ImportError:
            return False
        try:
            return bool(is_interrupted())
        except Exception:
            return False


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _error(text: str) -> str:
    return json.dumps({"error": text})


def _refresh_tool_list() -> None:
    """Makes this process build its tool list again: Hermes caches it, and a check_fn's answer isn't part of
    the cache key. Private names, so each step is optional."""
    try:
        from tools.registry import registry
        with registry._lock:
            registry._generation += 1
    except Exception:
        pass
    try:
        from model_tools import _clear_tool_defs_cache
        _clear_tool_defs_cache()
    except Exception:
        pass


def on_messaging_platform() -> bool:
    """Whether this turn came from a messaging platform (Telegram, Discord, ...), where others can talk to the
    bot. Unknown counts as no: the dashboard, Herald, Desktop, cron and the CLI don't set a platform."""
    try:
        from gateway.session_context import session_is_messaging_surface
    except ImportError:
        return False
    try:
        return bool(session_is_messaging_surface())
    except Exception:
        return False


class _Watch(threading.Thread):
    """Refreshes this process's tool list when another process records a phone's switches."""

    def __init__(self, path: Path):
        super().__init__(name="herald-push-phone-watch", daemon=True)
        self.path = path
        self.stopped = threading.Event()

    def _stamp(self):
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def run(self) -> None:
        last = self._stamp()
        while not self.stopped.wait(WATCH_SECONDS):
            now = self._stamp()
            if now != last:
                last = now
                _refresh_tool_list()


_watch_lock = threading.Lock()
_watch: Optional[_Watch] = None


def register_tools(ctx, phones: Phones) -> None:
    """Every phone tool, each offered only while a phone has it on."""
    global _watch
    try:
        from tools.registry import no_cache_check_fn
    except ImportError:  # older Hermes: the check is cached for 30 s
        def no_cache_check_fn(fn):
            return fn

    for name, (schema, _timeout, emoji) in TOOLS.items():
        def handler(args, session_id: str = "", _name=name, **_kwargs):
            if on_messaging_platform():
                return _error("Phone tools only work in chats from Herald, Desktop or the dashboard, not on a "
                              "messaging platform.")
            return phones.call(_name, args, session_id)

        def check(_name=name) -> bool:
            return phones.offered(_name)

        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=no_cache_check_fn(check), emoji=emoji)

    with _watch_lock:
        if _watch is None:
            _watch = _Watch(phones.root / "phones.json")
            _watch.start()
            started = _watch

            def stop() -> None:
                global _watch
                with _watch_lock:
                    if _watch is started:
                        _watch.stopped.set()
                        _watch = None

            on_unload = getattr(ctx, "on_unload", None)
            if on_unload is not None:
                on_unload(stop)
