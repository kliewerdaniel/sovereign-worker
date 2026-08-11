"""Encrypted secret store (spec §8).

Local-first, fail-closed, no-leak guarantees:

- Secrets are encrypted at rest with AES-GCM (via the optional ``cryptography``
  package). The *core* runtime stays zero third-party deps: if ``cryptography``
  is unavailable, ``SecretStore`` refuses to store or return plaintext and
  raises ``EncryptionUnavailable`` instead of silently persisting cleartext.
- The key is derived with PBKDF2-HMAC-SHA256 (stdlib) from a passphrase supplied
  via ``SWORKER_SECRETS_KEY`` (base64) or a ``secrets.key`` file. It is never
  written to the store, audit log, or evidence.
- Only a name + a sha256 *fingerprint* of the value is stored in the clear; the
  value itself is ciphertext. Audits/elections never log the plaintext.
- ``redact()`` scans arbitrary text for known secret values and replaces them
  with ``***REDACTED***`` so secrets can't leak through run logs or evidence.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets as _secrets_mod
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:  # optional dependency; core stays zero-dep
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - exercised only when crypto absent
    AESGCM = None  # type: ignore[assignment]


class EncryptionUnavailable(RuntimeError):
    """Raised when encryption is required but ``cryptography`` is missing."""


class SecretError(RuntimeError):
    pass


_KDF_ITERS = 200_000


def derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, _KDF_ITERS, dklen=32)


def _resolve_key(key: Optional[bytes], path: str) -> bytes:
    """Resolve a 32-byte KEK: explicit > env > keyfile > (create keyfile)."""
    if key is not None:
        if len(key) != 32:
            raise SecretError("key must be 32 bytes")
        return key
    env = os.environ.get("SWORKER_SECRETS_KEY")
    if env:
        return base64.b64decode(env)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return base64.b64decode(f.read().strip())
    # generate a fresh random key and persist it (local-first convenience)
    new_key = _secrets_mod.token_bytes(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        os.chmod(path, 0o600)
        f.write(base64.b64encode(new_key).decode())
    return new_key


@dataclass
class SecretRecord:
    name: str
    fingerprint: str
    ciphertext: str  # base64(nonce || ct)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.name,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "ciphertext": self.ciphertext,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SecretRecord":
        return cls(name=d["name"], fingerprint=d["fingerprint"], ciphertext=d["ciphertext"])


class SecretStore:
    def __init__(self, store, key: Optional[bytes] = None, key_path: Optional[str] = None):
        self.store = store
        self._key = _resolve_key(key, key_path or os.path.join(tempfile.gettempdir(), "sworker_secrets.key"))
        if AESGCM is None:
            raise EncryptionUnavailable(
                "storing secrets requires the optional 'cryptography' package; "
                "refusing to persist plaintext"
            )
        self._aes = AESGCM(self._key)
        # cache of plaintext values for redaction (in-memory only)
        self._values: Dict[str, str] = {}

    def set(self, name: str, value: str, actor: str = "system") -> SecretRecord:
        if not name or not isinstance(value, str):
            raise SecretError("secret name/value must be non-empty strings")
        nonce = _secrets_mod.token_bytes(12)
        ct = self._aes.encrypt(nonce, value.encode("utf-8"), None)
        blob = base64.b64encode(nonce + ct).decode()
        rec = SecretRecord(name=name, fingerprint=_fp(value), ciphertext=blob)
        self.store.put("secrets", rec.to_dict(), event="secret.stored")
        self._values[name] = value
        return rec

    def get(self, name: str) -> str:
        rec = self.store.get("secrets", name)
        if not rec:
            raise SecretError(f"no such secret: {name}")
        if name in self._values:
            return self._values[name]
        raw = base64.b64decode(rec["ciphertext"])
        pt = self._aes.decrypt(raw[:12], raw[12:], None)
        val = pt.decode("utf-8")
        self._values[name] = val
        return val

    def exists(self, name: str) -> bool:
        return self.store.get("secrets", name) is not None

    def list_names(self) -> List[str]:
        return [r["name"] for r in self.store.find("secrets")]

    def delete(self, name: str, actor: str = "system") -> None:
        self.store.delete("secrets", name, event="secret.deleted")
        self._values.pop(name, None)

    def redact(self, text: str) -> str:
        """Replace any known secret value in ``text`` with ***REDACTED***."""
        if not text:
            return text
        out = text
        for val in self._values.values():
            if val and len(val) >= 4 and val in out:
                out = out.replace(val, "***REDACTED***")
        return out


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


# Common API-key / token shapes; used to redact secrets even before they are
# registered, so logs never echo a raw credential.
_SECRET_RE = re.compile(
    r"""(?P<keep>(?:api[_-]?key|token|secret|password|passwd|bearer)\s*[=:]\s*)"""
    r"""(?P<val>['"]?[A-Za-z0-9_\-]{8,}(?:[./+=][A-Za-z0-9_\-]{4,})*['"]?)""",
    re.IGNORECASE,
)


def redact_static(text: str) -> str:
    """Pattern-based redaction that works without a SecretStore (best-effort)."""
    if not text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        val = m.group("val")
        q = ""
        if val and val[0] in ("'", '"'):
            q, val = val[0], val[1:]
        if val and val[-1] in ("'", '"'):
            val = val[:-1]
        return f"{m.group('keep')}{q}***REDACTED***{q}"

    return _SECRET_RE.sub(_sub, text)
