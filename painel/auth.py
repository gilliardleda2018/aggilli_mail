"""Senha do painel: hash scrypt guardado em painel/.senha (nunca a senha em texto)."""
import base64
import hashlib
import hmac
import os
from pathlib import Path

SENHA_FILE = Path(__file__).with_name(".senha")


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def save(password: str) -> None:
    salt = os.urandom(16)
    SENHA_FILE.write_text(
        base64.b64encode(salt).decode() + ":" + base64.b64encode(_hash(password, salt)).decode())
    SENHA_FILE.chmod(0o600)


def configured() -> bool:
    return SENHA_FILE.exists()


def check(password: str) -> bool:
    if not configured():
        return False
    salt_b64, hash_b64 = SENHA_FILE.read_text().strip().split(":")
    return hmac.compare_digest(_hash(password, base64.b64decode(salt_b64)),
                               base64.b64decode(hash_b64))
