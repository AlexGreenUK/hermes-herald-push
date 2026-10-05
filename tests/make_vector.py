"""Prints a gateway → phone test vector for Herald's Kotlin tests (PushInteropTest)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from herald_push import crypto  # noqa: E402

phone_enc = crypto.generate_key()
gateway_sig = crypto.generate_key()
topic = crypto.new_topic()
plaintext = '{"id":"v1","ts":1791170000,"type":"bot_message","bot":"test1","label":"Side","text":"über ✓"}'
envelope = crypto.seal(plaintext.encode(), topic, crypto.G2P, phone_enc.public_key(), gateway_sig)
print(json.dumps({
    "topic": topic,
    "plaintext": plaintext,
    "phone_enc_pkcs8": crypto.b64(phone_enc.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption())),
    "gateway_sig_pub": crypto.encode_public(gateway_sig.public_key()),
    "envelope": json.loads(envelope.to_json()),
}, indent=2, ensure_ascii=False))
