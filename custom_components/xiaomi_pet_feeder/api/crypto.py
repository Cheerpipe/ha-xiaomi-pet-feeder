"""Request signing and RC4 payload encryption used by the Xiaomi cloud gateway."""

from __future__ import annotations

import base64
import hashlib
import os
import time


def _rc4_keystream_xor(key: bytes, data: bytes) -> bytes:
    """RC4 with the first 1024 keystream bytes dropped (Mi Home variant)."""
    state = list(range(256))
    j = 0
    key_len = len(key)
    for i in range(256):
        j = (j + state[i] + key[i % key_len]) & 0xFF
        state[i], state[j] = state[j], state[i]

    i = j = 0
    for _ in range(1024):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]

    out = bytearray(len(data))
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out[n] = byte ^ state[(state[i] + state[j]) & 0xFF]
    return bytes(out)


def generate_nonce() -> str:
    """Return a base64 nonce: 8 random bytes + current minute (big endian)."""
    minutes = int(time.time() // 60)
    return base64.b64encode(os.urandom(8) + minutes.to_bytes(4, "big")).decode()


def signed_nonce(ssecurity: str, nonce: str) -> str:
    """Derive the per-request key from the account ssecurity and the nonce."""
    digest = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce))
    return base64.b64encode(digest.digest()).decode()


def sha1_signature(method: str, path: str, params: dict[str, str], key: str) -> str:
    """Signature used by the RC4 protocol (path without the /app prefix)."""
    if path.startswith("/app/"):
        path = path[4:]
    parts = [method.upper(), path, *(f"{k}={v}" for k, v in params.items()), key]
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def encrypt(key: str, plaintext: str) -> str:
    """Encrypt a string with the signed nonce, returning base64."""
    return base64.b64encode(_rc4_keystream_xor(base64.b64decode(key), plaintext.encode())).decode()


def decrypt(key: str, ciphertext: str) -> bytes:
    """Decrypt a base64 payload with the signed nonce."""
    return _rc4_keystream_xor(base64.b64decode(key), base64.b64decode(ciphertext))


def build_rc4_form(path: str, payload: str, ssecurity: str) -> tuple[dict[str, str], str]:
    """Build the encrypted form body for a POST to the /app gateway.

    Returns the form fields and the key needed to decrypt the response.
    """
    nonce = generate_nonce()
    key = signed_nonce(ssecurity, nonce)
    form: dict[str, str] = {"data": payload}
    form["rc4_hash__"] = sha1_signature("POST", path, form, key)
    form = {k: encrypt(key, v) for k, v in form.items()}
    form["signature"] = sha1_signature("POST", path, form, key)
    form["ssecurity"] = ssecurity
    form["_nonce"] = nonce
    return form, key
