"""Herald push cryptography, protocol v1 (see PROTOCOL.md).

P-256 ECDH to an ephemeral key, HKDF-SHA256, AES-256-GCM bound to the ntfy topic, and an ECDSA P-256
signature over everything, verified before anything is decrypted. Must stay byte-compatible with Herald's
PushCrypto.kt.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
G2P = "g2p"
P2G = "p2g"
_INFO = "herald-push/v1/"
_CURVE = ec.SECP256R1()


class Rejected(Exception):
    """An envelope that failed its signature or decryption: dropped, never acted on."""


@dataclass(frozen=True)
class Envelope:
    v: int
    epk: str
    salt: str
    iv: str
    ct: str
    sig: str

    def to_json(self) -> str:
        return json.dumps({"v": self.v, "epk": self.epk, "salt": self.salt, "iv": self.iv, "ct": self.ct, "sig": self.sig},
                          separators=(",", ":"))

    @staticmethod
    def from_json(body: str) -> "Envelope":
        try:
            data = json.loads(body)
            return Envelope(int(data["v"]), str(data["epk"]), str(data["salt"]), str(data["iv"]), str(data["ct"]), str(data["sig"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise Rejected("not an envelope") from exc


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise Rejected("bad base64") from exc


def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(_CURVE)


def encode_public(key: ec.EllipticCurvePublicKey) -> str:
    return b64(key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def decode_public(encoded: str) -> ec.EllipticCurvePublicKey:
    raw = unb64(encoded)
    if len(raw) != 65 or raw[0] != 0x04:
        raise Rejected("not an uncompressed P-256 point")
    try:
        # from_encoded_point validates that the point is on the curve (no invalid-curve attack).
        return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, raw)
    except ValueError as exc:
        raise Rejected("point not on P-256") from exc


def private_to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def private_from_pem(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("not an EC key")
    return key


def new_topic() -> str:
    """`hp-` and 128 random bits in hex: unguessable."""
    return "hp-" + secrets.token_hex(16)


def _signed_bytes(direction: str, topic: str, epk: str, salt: str, iv: str, ct: str) -> bytes:
    return "|".join(["herald-push/v1", direction, topic, epk, salt, iv, ct]).encode("utf-8")


def _derive(shared: bytes, salt: bytes, direction: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=(_INFO + direction).encode("utf-8")).derive(shared)


def seal(plaintext: bytes, topic: str, direction: str, recipient_enc: ec.EllipticCurvePublicKey,
         sender_sig: ec.EllipticCurvePrivateKey) -> Envelope:
    ephemeral = generate_key()
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive(ephemeral.exchange(ec.ECDH(), recipient_enc), salt, direction)
    ct = AESGCM(key).encrypt(iv, plaintext, topic.encode("utf-8"))
    epk, salt_b, iv_b, ct_b = encode_public(ephemeral.public_key()), b64(salt), b64(iv), b64(ct)
    sig = sender_sig.sign(_signed_bytes(direction, topic, epk, salt_b, iv_b, ct_b), ec.ECDSA(hashes.SHA256()))
    return Envelope(VERSION, epk, salt_b, iv_b, ct_b, b64(sig))


def open_envelope(envelope: Envelope, topic: str, direction: str, recipient_enc: ec.EllipticCurvePrivateKey,
                  sender_sig: ec.EllipticCurvePublicKey) -> bytes:
    if envelope.v != VERSION:
        raise Rejected(f"unknown version {envelope.v}")
    try:
        sender_sig.verify(unb64(envelope.sig), _signed_bytes(direction, topic, envelope.epk, envelope.salt, envelope.iv, envelope.ct),
                          ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise Rejected("bad signature") from exc
    try:
        key = _derive(recipient_enc.exchange(ec.ECDH(), decode_public(envelope.epk)), unb64(envelope.salt), direction)
        return AESGCM(key).decrypt(unb64(envelope.iv), unb64(envelope.ct), topic.encode("utf-8"))
    except Rejected:
        raise
    except Exception as exc:  # cryptography raises InvalidTag / ValueError
        raise Rejected("can't decrypt") from exc
