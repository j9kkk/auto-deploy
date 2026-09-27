"""Password hashing, session tokens and brute-force protection.

Only the standard library is used so the service has no hard dependency on a
crypto package being installable on the target host.  PBKDF2-HMAC-SHA256 with a
per-password salt is a well-understood choice for an admin password.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import string
import time
from typing import Any

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
TOKEN_BYTES = 32

# Passwords that must never survive the bootstrap step.
WEAK_PASSWORDS = {
    "admin",
    "admin123",
    "password",
    "123456",
    "12345678",
    "root",
    "changeme",
    "autodeploy",
}


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$iterations$salt_hex$hash_hex``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored hash."""
    if not password or not stored:
        return False
    try:
        algorithm, iterations_raw, salt_hex, hash_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses outdated parameters."""
    try:
        algorithm, iterations_raw, _salt, _hash = stored.split("$", 3)
    except ValueError:
        return True
    if algorithm != "pbkdf2_sha256":
        return True
    try:
        return int(iterations_raw) < PBKDF2_ITERATIONS
    except ValueError:
        return True


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """Hash a session token so the raw secret is never stored at rest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        # Guarantee at least one of each class so the password is not rejected
        # by the strength check in the settings UI.
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


def password_problem(password: str, minimum: int = 8) -> str | None:
    """Return a human-readable reason the password is unacceptable."""
    if len(password) < minimum:
        return f"密码长度至少 {minimum} 位"
    if password.lower() in WEAK_PASSWORDS:
        return "密码过于常见，请更换"
    classes = sum(
        (
            1 if any(c.islower() for c in password) else 0,
            1 if any(c.isupper() for c in password) else 0,
            1 if any(c.isdigit() for c in password) else 0,
            1 if any(not c.isalnum() for c in password) else 0,
        )
    )
    if classes < 2:
        return "密码需包含大小写字母、数字或符号中的至少两类"
    return None


class LoginThrottle:
    """In-memory, per-username failure counter with a cooldown window.

    Process-local on purpose: a single service instance owns the admin login, so
    there is nothing to share, and this keeps the service free of extra state.
    """

    def __init__(self, max_attempts: int, lockout_seconds: int) -> None:
        self.max_attempts = max(3, max_attempts)
        self.lockout_seconds = max(10, lockout_seconds)
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> None:
        window_start = now - self.lockout_seconds
        kept = [ts for ts in self._failures.get(key, []) if ts >= window_start]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def locked_for(self, key: str) -> int:
        """Seconds remaining before another attempt is allowed (0 = allowed)."""
        now = time.monotonic()
        until = self._locked_until.get(key)
        if until is None:
            return 0
        if until <= now:
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)
            return 0
        return int(until - now) + 1

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        attempts = self._failures.setdefault(key, [])
        attempts.append(now)
        if len(attempts) >= self.max_attempts:
            self._locked_until[key] = now + self.lockout_seconds

    def remaining_attempts(self, key: str) -> int:
        """How many more failures are allowed before a lockout."""
        self._prune(key, time.monotonic())
        return max(0, self.max_attempts - len(self._failures.get(key, [])))

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Strip secret fields before a user row is serialised to the client."""
    allowed = {"id", "username", "display_name", "is_admin", "created_at", "last_login_at", "password_changed_at"}
    result = {k: v for k, v in user.items() if k in allowed}
    # SQLite stores booleans as integers; expose a real JSON boolean.
    if "is_admin" in result:
        result["is_admin"] = bool(result["is_admin"])
    return result
