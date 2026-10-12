import json
import threading
import time

import pytest

from herald_push import commands, phone
from herald_push.phone import Phones
from herald_push.store import Store

PHONE = "0b6f2c1e-1111-4222-8333-944445555666"
OTHER = "99999999-aaaa-bbbb-cccc-dddddddddddd"


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
    assert phones.report(PHONE, "Pixel", ["phone_battery", "rm_rf", 5, "phone_battery"]) == ["phone_battery"]
    assert phones.offered("phone_battery")
    assert not phones.offered("phone_location")


def test_bad_device_id_is_refused(tmp_path):
    with pytest.raises(ValueError):
        Phones(Store(tmp_path)).report("../../etc", "x", ["phone_battery"])


def test_a_phone_not_heard_from_stops_offering(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, "Pixel", ["phone_battery"])
    now[0] += phone.STALE_SECONDS + 1
    assert not phones.offered("phone_battery")


def test_the_most_recent_phone_answers(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, "Pixel", ["phone_battery"])
    now[0] += 10
    phones.report(OTHER, "Tablet", ["phone_battery"])
    assert phones.phone_for("phone_battery") == OTHER


def test_a_call_waits_for_the_phone_and_cleans_up(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, "Pixel", ["phone_battery"])
    answer_when_asked(phones, PHONE, result={"level": 80})
    assert json.loads(phones.call("phone_battery", {}, "s1")) == {"level": 80}
    assert woken and woken[0][0] == PHONE
    assert list((tmp_path / "phone").glob("*.req.json")) == []
    assert list((tmp_path / "phone").glob("*.res.json")) == []


def test_pending_shows_the_call_to_its_phone_only(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, "Pixel", ["phone_location"])
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
    phones.report(PHONE, "Pixel", ["phone_battery"])
    assert "didn't answer" in json.loads(phones.call("phone_battery", {}))["error"]
    assert phones.pending(PHONE) == []


def test_turning_everything_off_refuses_what_waits(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, "Pixel", ["phone_battery"])

    def switch_off():
        deadline = time.time() + 5
        while time.time() < deadline and not phones.pending(PHONE):
            time.sleep(0.02)
        phones.report(PHONE, "Pixel", [])
    threading.Thread(target=switch_off, daemon=True).start()
    assert "turned this tool off" in json.loads(phones.call("phone_battery", {}))["error"]
    assert not phones.offered("phone_battery")


def test_an_unknown_request_id_is_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    assert phones.answer(PHONE, "../../identity", {}, None) is False
    assert phones.answer(PHONE, "0" * 32, {}, None) is False


def test_a_huge_answer_becomes_an_error(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, "Pixel", ["phone_contacts"])
    answer_when_asked(phones, PHONE, result={"x": "a" * (phone.MAX_RESULT + 10)})
    assert "too long" in json.loads(phones.call("phone_contacts", {"query": "a"}))["error"]


def test_phone_commands(tmp_path):
    phones = Phones(Store(tmp_path))
    handle = commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: True, phones=phones)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "name": "Pixel",
                                                           "tools": ["phone_battery", "nope"]})))
    assert reply == {"ok": True, "tools": ["phone_battery"]}
    assert json.loads(handle("phone pending " + PHONE)) == {"ok": True, "requests": []}
    assert json.loads(handle("phone answer {"))["ok"] is False
    assert json.loads(handle("phone answer " + json.dumps({"device_id": PHONE, "id": "0" * 32})))["ok"] is False
    assert "phone_battery" in json.loads(handle("info"))["phone_tools"]


def test_phone_commands_refuse_untrusted_surfaces(tmp_path):
    phones = Phones(Store(tmp_path))
    handle = commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: False, phones=phones)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "tools": ["phone_battery"]})))
    assert reply["ok"] is False and not phones.offered("phone_battery")


def test_a_big_answer_fits_through_the_command(tmp_path):
    handle = commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: True,
                                   phones=Phones(Store(tmp_path)))
    big = json.dumps({"device_id": PHONE, "id": "0" * 32, "result": "a" * 20000})
    assert json.loads(handle("phone answer " + big)).get("error") != "too long"
    assert json.loads(handle("register " + "a" * 20000))["error"] == "too long"


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji=""):
        assert toolset == phone.TOOLSET and schema["name"] == name
        self.tools[name] = (handler, check_fn)


def test_tools_are_offered_only_while_a_phone_has_them_on(tmp_path):
    ctx, phones = FakeCtx(), Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    assert set(ctx.tools) == set(phone.TOOLS)
    handler, check = ctx.tools["phone_battery"]
    assert check() is False
    phones.report(PHONE, "Pixel", ["phone_battery"])
    assert check() is True and ctx.tools["phone_network"][1]() is False
    answer_when_asked(phones, PHONE, result={"level": 5})
    # The dispatcher passes context kwargs the handler doesn't use.
    assert json.loads(handler({}, session_id="s", task_id="t", user_task="u")) == {"level": 5}


def test_every_schema_is_a_closed_object():
    for name, (schema, timeout, _) in phone.TOOLS.items():
        assert schema["name"] == name and name.startswith("phone_")
        assert schema["parameters"]["additionalProperties"] is False
        assert timeout in (phone.READ_TIMEOUT, phone.CONFIRM_TIMEOUT)
