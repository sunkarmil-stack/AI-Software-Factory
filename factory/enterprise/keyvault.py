"""Azure Key Vault signing and secrets (production profile).

Signing keys are non-exportable EC P-256 keys (HSM-backed with Premium/Managed HSM). The factory
sends a SHA-256 digest to `keys/{name}/sign` (ES256) and verifies locally with the public key
from `keys/{name}`. Only identities granted "Key Vault Crypto User" on a key can sign with it, so
agents cannot sign even if they reach Key Vault. Same interface as core.crypto.KeyStore.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, encode_dss_signature

from ..core.crypto import TRUSTED_SIGNERS, SigningDenied


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class KeyVaultClient:
    def __init__(self, url: str, token_provider, audience: str = "https://vault.azure.net", api_version: str = "7.4"):
        self.url, self.tp, self.aud, self.v = url.rstrip("/"), token_provider, audience, api_version

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"{self.url}/{path}?api-version={self.v}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"authorization": f"Bearer {self.tp.token(self.aud)}",
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise PermissionError(f"Key Vault {method} {path}: {e.code} {e.read()[:200]!r}")


class KeyVaultSigner:
    def __init__(self, client: KeyVaultClient, key_map: dict[str, str]):
        self.kv, self.keys = client, key_map
        self._pub: dict[str, ec.EllipticCurvePublicKey] = {}
        self._lock = threading.Lock()

    def ensure(self, ident: str) -> None:
        if ident not in TRUSTED_SIGNERS:
            raise SigningDenied(f"{ident} is not permitted to hold a signing key")
        if ident not in self.keys:
            raise SigningDenied(f"no Key Vault key mapped for {ident}")

    def sign(self, ident: str, data: bytes) -> str:
        self.ensure(ident)
        r = self.kv.call("POST", f"keys/{self.keys[ident]}/sign",
                         {"alg": "ES256", "value": b64u(hashlib.sha256(data).digest())})
        return "kv:" + r["value"]   # raw r||s, base64url

    def _public(self, ident: str):
        with self._lock:
            if ident not in self._pub:
                jwk = self.kv.call("GET", f"keys/{self.keys[ident]}")["key"]
                nums = ec.EllipticCurvePublicNumbers(int.from_bytes(ub64u(jwk["x"]), "big"),
                                                     int.from_bytes(ub64u(jwk["y"]), "big"), ec.SECP256R1())
                self._pub[ident] = nums.public_key()
            return self._pub[ident]

    def verify(self, ident: str, data: bytes, sig: str) -> bool:
        if not sig or not sig.startswith("kv:") or ident not in self.keys:
            return False
        raw = ub64u(sig[3:])
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        try:
            self._public(ident).verify(der, hashlib.sha256(data).digest(), ec.ECDSA(Prehashed(hashes.SHA256())))
            return True
        except InvalidSignature:
            return False


class KeyVaultSecrets:
    def __init__(self, client: KeyVaultClient):
        self.kv = client

    def get(self, name: str, default: str = "") -> str:
        try:
            return self.kv.call("GET", f"secrets/{name}")["value"]
        except PermissionError:
            return default
