"""Encrypts the passwords a shared console remembers for each browser
(2026-11-23; before that they lay in operators/<id>/info.env as plain text).

The key comes from the browser's own workspace cookie, which is never written
to disk — only its hash is, as the name of the workspace's folder. So the
saved passwords can't be read from the server's files alone: not from a copy
or a backup of them, not by root. A password opens only while its own browser
is making a request, which is the only time the console needs it (every CRM
and Webitel login is started by the page).

Not covered: someone who can change the console's code or read its memory
while that browser is using it, and the Webitel sessions in .auth/, which
stay plain files until they expire.

AES-256-GCM; the key is HKDF-SHA256 of the cookie (128 random bits, so no
slow password hashing is needed). The workspace and the setting's name are
authenticated along with the value, so an encrypted password moved to
another workspace or another setting doesn't open.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "enc1:"
_NONCE_BYTES = 12
_KEY_INFO = b"crm-ai-agent console: saved passwords v1"


class SecretError(Exception):
    """A saved password that doesn't open: damaged, or not this browser's."""


def workspace_key(token: str) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=_KEY_INFO).derive(bytes.fromhex(token))


def is_sealed(value: str) -> bool:
    return value.startswith(_PREFIX)


def seal(key: bytes, workspace: str, name: str, value: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, value.encode("utf-8"), _context(workspace, name))
    return _PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def unseal(key: bytes, workspace: str, name: str, stored: str) -> str:
    try:
        raw = base64.b64decode(stored.removeprefix(_PREFIX), altchars=b"-_", validate=True)
        opened = AESGCM(key).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _context(workspace, name))
        return opened.decode("utf-8")
    except (binascii.Error, InvalidTag, ValueError) as exc:
        raise SecretError(f"{name} doesn't open") from exc


def _context(workspace: str, name: str) -> bytes:
    return f"{workspace}\n{name}".encode("utf-8")
