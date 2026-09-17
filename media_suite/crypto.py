"""Encryption of stored provider keys + signed URLs for media served to the dashboard iframe."""
import base64
import hashlib
import hmac
import time

from cryptography.fernet import Fernet, InvalidToken

from .settings import settings


def _key() -> bytes:
    if not settings.secret_key:
        raise RuntimeError("SECRET_KEY is not set")
    return base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())


def encrypt(text: str) -> str:
    return Fernet(_key()).encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("stored key cannot be decrypted (SECRET_KEY changed?)")


def sign(value: str, ttl: int = 3600) -> str:
    exp = str(int(time.time()) + ttl)
    mac = hmac.new(settings.secret_key.encode(), f"{value}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def verify(value: str, sig: str) -> bool:
    try:
        exp, mac = sig.split(".")
    except ValueError:
        return False
    if int(exp) < time.time():
        return False
    expected = hmac.new(settings.secret_key.encode(), f"{value}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, mac)
