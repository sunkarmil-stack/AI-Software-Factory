"""Signing keys for trusted services (REQ 041, 042, 045).

Only trusted workload identities hold signing keys. In the Azure reference deployment
these map to Key Vault / Managed HSM keys bound to workload identities; here they are
Ed25519 keys on disk in a directory never mounted into sandboxes.
"""
from __future__ import annotations

import base64
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

TRUSTED_SIGNERS = {"ci-builder", "release-service", "skill-registry", "approval-service", "policy-service"}


class SigningDenied(PermissionError):
    pass


class KeyStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "public").mkdir(exist_ok=True)

    def _priv_path(self, ident: str) -> Path:
        return self.root / f"{ident}.key"

    def ensure(self, ident: str) -> None:
        if ident not in TRUSTED_SIGNERS and not ident.startswith("human:"):
            raise SigningDenied(f"{ident} is not permitted to hold a signing key")
        p = self._priv_path(ident)
        if p.exists():
            return
        k = Ed25519PrivateKey.generate()
        p.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        p.chmod(0o600)
        (self.root / "public" / f"{ident}.pub").write_bytes(k.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign(self, ident: str, data: bytes) -> str:
        if ident not in TRUSTED_SIGNERS and not ident.startswith("human:"):
            raise SigningDenied(f"{ident} cannot sign")
        self.ensure(ident)
        k = serialization.load_pem_private_key(self._priv_path(ident).read_bytes(), None)
        return base64.b64encode(k.sign(data)).decode()

    def verify(self, ident: str, data: bytes, sig: str) -> bool:
        p = self.root / "public" / f"{ident}.pub"
        if not p.exists() or not sig:
            return False
        pub: Ed25519PublicKey = serialization.load_pem_public_key(p.read_bytes())  # type: ignore
        try:
            pub.verify(base64.b64decode(sig), data)
            return True
        except (InvalidSignature, ValueError):
            return False
