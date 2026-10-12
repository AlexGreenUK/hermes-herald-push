import json
import os
import threading
import time

import pytest

from herald_push import commands, phone
from herald_push.phone import Phones
from herald_push.store import Store

PHONE = "0b6f2c1e-1111-4222-8333-944445555666"
OTHER = "99999999-aaaa-bbbb-cccc-dddddddddddd"
KEY = "a" * 64
OTHER_KEY = "b" * 64


def answer_when_asked(phones, device_id, result=None, error=None, wait=5.0):
    """Answers the first request for [device_id], as Herald would, from another thread."""
    def run():
        deadline = time.time() + wait
        while time.time() < deadline:
            waiting = phones.pending(device_id)
            if waiting:
                phones.answer(device_id, waiting[0]["id"], result, error)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_only_known_tools_are_kept_and_offered(tmp_path):
    phones = Phones(Store(tmp_path))
    assert phones.report(PHONE, KEY, "Pixel", ["phone_battery", "rm_rf", 5, "phone_battery"]) == ["phone_battery"]
    assert phones.offered("phone_battery")
    assert not phones.offered("phone_location")


@pytest.mark.parametrize("device_id, key, push_id", [
    ("../../etc", KEY, None), (PHONE, "short", None), (PHONE, "Z" * 64, None), (PHONE, KEY, "../x"),
])
def test_bad_ids_and_keys_are_refused(tmp_path, device_id, key, push_id):
    with pytest.raises(ValueError):
        Phones(Store(tmp_path)).report(device_id, key, "x", ["phone_battery"], push_id)


def test_no_push_registration_no_nudge(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    answer_when_asked(phones, PHONE, result={})
    phones.call("phone_battery", {})
    assert woken == []


def test_another_key_cant_take_over_a_phone(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    with pytest.raises(ValueError):
        phones.report(PHONE, OTHER_KEY, "Thief", [])
    # Turning everything off keeps the key bound to the id.
    phones.report(PHONE, KEY, "Pixel", [])
    with pytest.raises(ValueError):
        phones.report(PHONE, OTHER_KEY, "Thief", ["phone_battery"])
    assert phones.verify(PHONE, KEY) and not phones.verify(PHONE, OTHER_KEY)
    assert "key" not in json.dumps(phones.pending(PHONE))
    stored = (tmp_path / "phone" / "phones.json").read_text()
    assert KEY not in stored


def test_a_phone_not_heard_from_stops_offering(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    now[0] += phone.STALE_SECONDS + 1
    assert not phones.offered("phone_battery")


def test_the_most_recent_phone_answers(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    now[0] += 10
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    assert phones.phone_for("phone_battery") == OTHER


def test_a_call_waits_for_the_phone_and_cleans_up(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"], push_id=OTHER)
    answer_when_asked(phones, PHONE, result={"level": 80})
    assert json.loads(phones.call("phone_battery", {}, "s1")) == {"level": 80}
    # The nudge goes to the phone's push registration.
    deadline = time.time() + 2
    while not woken and time.time() < deadline:
        time.sleep(0.02)
    assert woken and woken[0][0] == OTHER
    assert list((tmp_path / "phone").glob("*.req.json")) == []
    assert list((tmp_path / "phone").glob("*.res.json")) == []


def test_pending_shows_the_call_to_its_phone_only(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_location"])
    seen = {}

    def peek():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(PHONE)
            if waiting:
                seen["mine"], seen["other"] = waiting, phones.pending(OTHER)
                # Another phone can't answer it.
                seen["stolen"] = phones.answer(OTHER, waiting[0]["id"], {"lat": 0}, None)
                phones.answer(PHONE, waiting[0]["id"], None, "Location is off.")
                return
            time.sleep(0.02)
    threading.Thread(target=peek, daemon=True).start()
    result = json.loads(phones.call("phone_location", {"fresh": True}, "chat-7"))
    assert result == {"error": "Location is off."}
    assert seen["other"] == [] and seen["stolen"] is False
    assert seen["mine"][0]["tool"] == "phone_location"
    assert seen["mine"][0]["args"] == {"fresh": True} and seen["mine"][0]["session"] == "chat-7"


def test_no_answer_times_out(tmp_path, monkeypatch):
    schema, _, emoji = phone.TOOLS["phone_battery"]
    monkeypatch.setitem(phone.TOOLS, "phone_battery", (schema, 0.3, emoji))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert "didn't answer" in json.loads(phones.call("phone_battery", {}))["error"]
    assert phones.pending(PHONE) == []


def test_stop_ends_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(Phones, "interrupted", staticmethod(lambda: True))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_open"])
    started = time.time()
    assert "Stopped" in json.loads(phones.call("phone_open", {"kind": "url", "value": "https://x"}))["error"]
    assert time.time() - started < 5


def test_turning_a_tool_off_refuses_what_waits_for_it(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery", "phone_network"])

    def switch_off():
        deadline = time.time() + 5
        while time.time() < deadline and not phones.pending(PHONE):
            time.sleep(0.02)
        phones.report(PHONE, KEY, "Pixel", ["phone_network"])
    threading.Thread(target=switch_off, daemon=True).start()
    assert "turned this tool off" in json.loads(phones.call("phone_battery", {}))["error"]
    assert not phones.offered("phone_battery") and phones.offered("phone_network")


def test_a_chat_that_still_has_a_tool_gets_a_refusal(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", [])
    assert "No phone offers" in json.loads(phones.call("phone_battery", {}))["error"]


def test_an_unknown_request_id_is_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    assert phones.answer(PHONE, "../../identity", {}, None) is False
    assert phones.answer(PHONE, "0" * 32, {}, None) is False


def test_a_late_answer_leaves_nothing_behind(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.root.mkdir(parents=True)
    rid = "1" * 32
    # Written as the tool gave up: the request is already gone by the time the answer lands.
    (phones.root / f"{rid}.req.json").write_text(json.dumps({"id": rid, "device_id": PHONE, "expires": 2**40}))
    real_write = phone._write_private

    def write_then_lose_request(path, data):
        real_write(path, data)
        (phones.root / f"{rid}.req.json").unlink()
    phone._write_private = write_then_lose_request
    try:
        assert phones.answer(PHONE, rid, {"x": 1}, None) is False
    finally:
        phone._write_private = real_write
    assert list(phones.root.glob("*.json")) == []


def test_leftovers_are_swept(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.root.mkdir(parents=True)
    old = time.time() - phone.LEFTOVER_SECONDS - 10
    for name in ("2" * 32 + ".req.json", "3" * 32 + ".res.json", ".tmp-x"):
        path = phones.root / name
        path.write_text("{}")
        os.utime(path, (old, old))
    phones.pending(PHONE)
    assert [p.name for p in phones.root.iterdir()] == []


def test_a_huge_answer_becomes_an_error(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_contacts"])
    answer_when_asked(phones, PHONE, result={"x": "a" * (phone.MAX_RESULT + 10)})
    assert "too long" in json.loads(phones.call("phone_contacts", {"query": "a"}))["error"]


def handler_for(tmp_path, trusted=True):
    phones = Phones(Store(tmp_path))
    return commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: trusted, phones=phones), phones


def test_phone_commands(tmp_path):
    handle, _ = handler_for(tmp_path)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": KEY, "name": "Pixel",
                                                           "tools": ["phone_battery", "nope"]})))
    assert reply == {"ok": True, "tools": ["phone_battery"]}
    assert json.loads(handle("phone pending " + json.dumps({"device_id": PHONE, "key": KEY}))) == {"ok": True, "requests": []}
    assert json.loads(handle("phone answer {"))["ok"] is False
    answer = {"device_id": PHONE, "key": KEY, "id": "0" * 32}
    assert json.loads(handle("phone answer " + json.dumps(answer)))["ok"] is False
    assert "phone_battery" in json.loads(handle("info"))["phone_tools"]


def test_phone_commands_need_the_phones_key(tmp_path):
    handle, phones = handler_for(tmp_path)
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    for op in ("pending", "answer"):
        reply = json.loads(handle(f"phone {op} " + json.dumps({"device_id": PHONE, "key": OTHER_KEY, "id": "0" * 32})))
        assert reply == {"ok": False, "error": "unknown phone"}
    taken = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": OTHER_KEY, "tools": []})))
    assert taken["ok"] is False and phones.offered("phone_battery")


def test_phone_commands_refuse_untrusted_surfaces(tmp_path):
    handle, phones = handler_for(tmp_path, trusted=False)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": KEY, "tools": ["phone_battery"]})))
    assert reply["ok"] is False and not phones.offered("phone_battery")


def test_a_big_answer_fits_through_the_command(tmp_path):
    handle, _ = handler_for(tmp_path)
    big = json.dumps({"device_id": PHONE, "key": KEY, "id": "0" * 32, "result": "a" * 20000})
    assert json.loads(handle("phone answer " + big)).get("error") != "too long"
    assert json.loads(handle("register " + "a" * 20000))["error"] == "too long"


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.unloads = []

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji=""):
        assert toolset == phone.TOOLSET and schema["name"] == name
        self.tools[name] = (handler, check_fn)

    def on_unload(self, callback):
        self.unloads.append(callback)


@pytest.fixture
def ctx():
    ctx = FakeCtx()
    yield ctx
    for stop in ctx.unloads:
        stop()


def test_tools_are_offered_only_while_a_phone_has_them_on(tmp_path, ctx):
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    assert set(ctx.tools) == set(phone.TOOLS)
    handler, check = ctx.tools["phone_battery"]
    assert check() is False
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert check() is True and ctx.tools["phone_network"][1]() is False
    answer_when_asked(phones, PHONE, result={"level": 5})
    # The dispatcher passes context kwargs the handler doesn't use.
    assert json.loads(handler({}, session_id="s", task_id="t", user_task="u")) == {"level": 5}


def test_messaging_platforms_get_no_phone_tools(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "on_messaging_platform", lambda: True)
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", ["phone_location"])
    assert "messaging platform" in json.loads(ctx.tools["phone_location"][0]({}))["error"]
    assert phones.pending(PHONE) == []


def test_another_process_changing_switches_refreshes_the_tool_list(tmp_path, ctx, monkeypatch):
    refreshed = []
    monkeypatch.setattr(phone, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(phone, "_refresh_tool_list", lambda: refreshed.append(1))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", [])
    phone.register_tools(ctx, phones)
    refreshed.clear()
    time.sleep(0.2)
    (phones.root / "phones.json").write_text("{}")
    deadline = time.time() + 3
    while not refreshed and time.time() < deadline:
        time.sleep(0.02)
    assert refreshed


def test_every_schema_is_a_closed_object():
    for name, (schema, timeout, _) in phone.TOOLS.items():
        assert schema["name"] == name and name.startswith("phone_")
        assert schema["parameters"]["additionalProperties"] is False
        assert timeout in (phone.READ_TIMEOUT, phone.CONFIRM_TIMEOUT)
