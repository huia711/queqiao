"""Optional password protection for self-hosted deployments.

Enabled when QUEQIAO_AUTH_HASH is set, in the form
``pbkdf2_sha256$<iterations>$<salt hex>$<digest hex>``. Generate one with ``python -m app.auth``.
QUEQIAO_AUTH_SECRET signs session cookies; without it a random per-process key is used, so
sessions end when the service restarts.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from collections import deque

from .config import settings

COOKIE = "queqiao_session"
MAX_FAILURES = 8
FAILURE_WINDOW = 600
MAX_PASSWORD_LENGTH = 1024
_PROCESS_SECRET = secrets.token_hex(32)


def enabled() -> bool:
    return bool((settings.auth_hash or "").strip())


def hash_password(password: str, iterations: int = 600_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str) -> bool:
    if not password or len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        scheme, iterations, salt_hex, digest_hex = settings.auth_hash.strip().split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(candidate.hex(), digest_hex.strip().lower())
    except (TypeError, ValueError):
        return False


def _signature(expires: str) -> str:
    key = (settings.auth_secret or _PROCESS_SECRET).encode("utf-8")
    return hmac.new(key, f"queqiao-session:{expires}".encode("ascii"), hashlib.sha256).hexdigest()


def issue_session() -> str:
    expires = str(int(time.time()) + int(settings.auth_max_age))
    return f"{expires}.{_signature(expires)}"


def session_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expires, signature = value.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _signature(expires))


class LoginThrottle:
    """Refuse further attempts from one client after repeated wrong passwords."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str, now: float) -> deque[float]:
        entries = self._failures.setdefault(key, deque())
        while entries and now - entries[0] > FAILURE_WINDOW:
            entries.popleft()
        return entries

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= MAX_FAILURES

    def failed(self, key: str) -> None:
        with self._lock:
            now = time.time()
            self._recent(key, now).append(now)
            for stale in [name for name, entries in self._failures.items() if not entries]:
                del self._failures[stale]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


throttle = LoginThrottle()


def main() -> None:
    first = getpass.getpass("新的访问密码：")
    second = getpass.getpass("再输入一次：")
    if not first or first != second:
        sys.exit("两次输入不一致或为空")
    print(hash_password(first))


if __name__ == "__main__":
    main()
