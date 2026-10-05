import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import load_der_private_key

from herald_push import crypto


def test_round_trip_both_ways():
    phone_enc, phone_sig, gw_enc, gw_sig = (crypto.generate_key() for _ in range(4))
    topic = crypto.new_topic()
    env = crypto.seal(b"hello", topic, crypto.G2P, phone_enc.public_key(), gw_sig)
    assert crypto.open_envelope(crypto.Envelope.from_json(env.to_json()), topic, crypto.G2P, phone_enc, gw_sig.public_key()) == b"hello"
    back = crypto.seal(b"reply", topic, crypto.P2G, gw_enc.public_key(), phone_sig)
    assert crypto.open_envelope(back, topic, crypto.P2G, gw_enc, phone_sig.public_key()) == b"reply"


def test_forgery_tampering_topic_and_direction_are_refused():
    phone_enc, gw_sig, impostor = (crypto.generate_key() for _ in range(3))
    topic = crypto.new_topic()
    env = crypto.seal(b"x", topic, crypto.G2P, phone_enc.public_key(), gw_sig)
    forged = crypto.seal(b"x", topic, crypto.G2P, phone_enc.public_key(), impostor)
    for bad, on_topic, direction in [
        (forged, topic, crypto.G2P),
        (crypto.Envelope(env.v, env.epk, env.salt, env.iv, env.ct[:-2] + ("A" if env.ct[-2] != "A" else "B") + env.ct[-1], env.sig), topic, crypto.G2P),
        (env, crypto.new_topic(), crypto.G2P),
        (env, topic, crypto.P2G),
        (crypto.Envelope(2, env.epk, env.salt, env.iv, env.ct, env.sig), topic, crypto.G2P),
    ]:
        with pytest.raises(crypto.Rejected):
            crypto.open_envelope(bad, on_topic, direction, phone_enc, gw_sig.public_key())


def test_bad_public_keys_are_refused():
    good = crypto.encode_public(crypto.generate_key().public_key())
    raw = bytearray(crypto.unb64(good))
    raw[64] ^= 1
    for bad in [crypto.b64(bytes(raw)), crypto.b64(bytes(33)), "!!!"]:
        with pytest.raises(crypto.Rejected):
            crypto.decode_public(bad)


def test_opens_what_herald_sealed():
    """Herald's PushInteropTest writes this vector; run it first (it lives in Herald's build dir)."""
    path = Path(os.environ.get("HERALD_P2G_VECTOR", "../hermes-kotlin/shared/core/build/push-vector-p2g.json"))
    if not path.exists():
        pytest.skip("no vector from Herald yet")
    vector = json.loads(path.read_text(encoding="utf-8"))
    gw_enc = load_der_private_key(crypto.unb64(vector["gateway_enc_pkcs8"]), password=None)
    env = crypto.Envelope.from_json(json.dumps(vector["envelope"]))
    opened = crypto.open_envelope(env, vector["topic"], crypto.P2G, gw_enc, crypto.decode_public(vector["phone_sig_pub"]))
    assert opened.decode() == vector["plaintext"]
