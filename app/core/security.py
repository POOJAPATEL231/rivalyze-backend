"""Password hashing + JWT issue/verify. The one place crypto lives.

Rules baked in:
  - Passwords are hashed with bcrypt (per-password salt, deliberately slow).
    Plaintext is never stored, logged, or returned.
  - bcrypt silently ignores bytes past 72; we truncate to 72 bytes IDENTICALLY
    on hash and verify so behavior is consistent and never raises on long input.
  - JWTs are HS256, signed with config.JWT_SECRET, and always carry `exp` + `iat`.
    decode_access_token verifies signature AND expiry (raises on either).
  - DUMMY_HASH lets the login path run a real verify even when the email is
    unknown, so response timing doesn't leak whether an account exists.
"""
import datetime as dt
import hashlib
import secrets

import bcrypt
import jwt

from . import config

_BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Precomputed once at import: a real hash of a throwaway value. Verifying a
# login attempt against this when the user is not found keeps the failure path
# as slow as the success path (timing-attack / user-enumeration mitigation).
DUMMY_HASH: str = hash_password("timing-attack-mitigation-dummy")


def create_access_token(subject: str, email: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": subject,
        "email": email,
        "iat": now,
        "exp": now + dt.timedelta(minutes=config.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Return the validated claims. Raises jwt.PyJWTError (ExpiredSignatureError,
    InvalidTokenError, ...) on any signature/expiry/format problem — callers
    convert that into a 401, never a 500."""
    return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])


# ---------------------------- refresh tokens ----------------------------
# A refresh token is a high-entropy opaque random string (NOT a JWT): the
# server holds the authority via a stored hash, so it can be revoked. Because
# it's high-entropy we hash with plain SHA-256 (fast, no salt needed) — bcrypt
# is only for low-entropy human passwords.
def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_expiry() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=config.REFRESH_TOKEN_EXPIRE_DAYS)


def refresh_is_expired(expires_at: dt.datetime) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    # tolerate a naive datetime (shouldn't happen: PG returns tz-aware, and we
    # store tz-aware) by assuming UTC rather than raising on the compare.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    return expires_at <= now
