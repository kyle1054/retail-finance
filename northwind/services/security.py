"""Lightweight in-memory login throttling (brute-force protection).

Tracks recent failed login attempts per key (IP + identifier) and locks the key
out after too many failures inside a rolling window. State is in-process, which
is correct for a single-worker deployment (see the gunicorn recipe in
requirements.txt); if the app is ever scaled to multiple workers this should move
to a shared store (DB/Redis).

Keying by IP *and* identifier means a wrong-guess storm against one account from
one source gets blocked without letting an attacker lock a colleague out of their
account globally. Behind a reverse proxy, remote_addr must be the real client IP
(configure ProxyFix / trusted X-Forwarded-For) for this to be effective.
"""
import time
import threading

MAX_ATTEMPTS = 5          # failures allowed inside the window before lockout
WINDOW_SECONDS = 900      # 15 min: both the counting window and the lockout span

# Minimum length for any human-chosen account password (admins, and admin-typed
# cardholder/RM passwords). 10+ chars meaningfully raises brute-force cost without
# forcing complexity rules that push people toward weaker, reused secrets. Single
# source of truth so every place that sets a password enforces the same floor.
MIN_PASSWORD_LENGTH = 10

_lock = threading.Lock()
_failures = {}            # key -> list[float] of recent failure timestamps


def _prune(key, now):
    cutoff = now - WINDOW_SECONDS
    kept = [t for t in _failures.get(key, ()) if t > cutoff]
    if kept:
        _failures[key] = kept
    else:
        _failures.pop(key, None)
    return kept


def seconds_locked(key):
    """Seconds until `key` may try again, or 0 if it is not currently locked."""
    now = time.time()
    with _lock:
        attempts = _prune(key, now)
        if len(attempts) >= MAX_ATTEMPTS:
            return max(int(attempts[-1] + WINDOW_SECONDS - now), 1)
        return 0


def record_failure(key):
    """Register one failed attempt for `key`."""
    now = time.time()
    with _lock:
        _failures.setdefault(key, []).append(now)


def reset(key):
    """Clear a key's failures (call on successful login)."""
    with _lock:
        _failures.pop(key, None)


def make_key(scope, identifier, ip):
    """Build a throttling key from a scope, a user identifier and client IP."""
    return f"{scope}:{(ip or '?')}:{(identifier or '').strip().lower()}"
