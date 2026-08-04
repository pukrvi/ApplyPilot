"""
Time-limited API keys.

Keys written to ~/.applypilot/.env are stamped with the date they first
appeared. Once a key passes its TTL (default 30 days) it is scrubbed from
the .env file and from the current process environment, and the user is
told to run a fresh setup.

Design notes:
  * The key value itself is never stored in the metadata file. We keep a
    truncated SHA-256 so we can notice that a key was rotated by hand
    (different hash -> restart the clock) without ever writing the secret
    to a second location on disk.
  * A key found with no stamp is treated as first-seen, not as expired.
    That keeps manual `echo KEY=... >> .env` edits working: the clock
    starts the next time ApplyPilot runs.
  * LLM_URL is deliberately not managed. A local endpoint is not a secret
    and must not stop working after 30 days.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta

from . import config

# Secrets subject to expiry. LLM_URL / LLM_MODEL are config, not secrets.
MANAGED_KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "CAPSOLVER_API_KEY", "LLM_API_KEY")

META_PATH = config.APP_DIR / ".keymeta.json"

DEFAULT_TTL_DAYS = 30


def _today() -> date:
    """Today's date in UTC.

    _today() reads the local clock, so a key set at 23:00 in one timezone
    could appear a day older or younger than intended. UTC keeps the TTL
    stable regardless of where the machine is.
    """
    return datetime.now(UTC).date()


def ttl_days() -> int:
    """Key lifetime in days. Override with APPLYPILOT_KEY_TTL_DAYS."""
    raw = os.environ.get("APPLYPILOT_KEY_TTL_DAYS", "")
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_TTL_DAYS
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def _fingerprint(value: str) -> str:
    """Truncated hash so we can detect rotation without storing the secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _read_meta() -> dict:
    if not META_PATH.exists():
        return {}
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(meta: dict) -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    try:
        META_PATH.chmod(0o600)
    except OSError:
        pass


def stamp(var: str, value: str, when: date | None = None) -> None:
    """Record that `var` was set today (or on `when`)."""
    meta = _read_meta()
    meta[var] = {
        "set_at": (when or _today()).isoformat(),
        "fingerprint": _fingerprint(value),
    }
    _write_meta(meta)


def forget(var: str) -> None:
    """Drop stored metadata for `var`."""
    meta = _read_meta()
    if meta.pop(var, None) is not None:
        _write_meta(meta)


def expires_on(var: str) -> date | None:
    """Date `var` expires, or None if it has no stamp."""
    entry = _read_meta().get(var)
    if not entry:
        return None
    try:
        set_at = datetime.fromisoformat(entry["set_at"]).date()
    except (KeyError, ValueError):
        return None
    return set_at + timedelta(days=ttl_days())


def days_remaining(var: str) -> int | None:
    """Whole days until `var` expires. Negative once overdue."""
    due = expires_on(var)
    if due is None:
        return None
    return (due - _today()).days


def scrub_from_env_file(var: str, note: str | None = None) -> bool:
    """Remove `var`'s line from the .env file, preserving everything else.

    Args:
        var: Environment variable name to remove.
        note: Comment explaining the removal. Defaults to TTL expiry wording.

    Returns True if a line was actually removed.
    """
    if not config.ENV_PATH.exists():
        return False

    try:
        lines = config.ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    kept: list[str] = []
    removed = False
    for line in lines:
        stripped = line.lstrip()
        # Only match real assignments, never commented-out examples.
        if not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == var:
            removed = True
            continue
        kept.append(line)

    if not removed:
        return False

    body = "".join(kept).rstrip("\n")
    reason = note or f"expired after {ttl_days()} days and was removed automatically"
    trailer = f"\n# {var} {reason}.\n{var}=\n"
    config.ENV_PATH.write_text(body + "\n" + trailer, encoding="utf-8")
    try:
        config.ENV_PATH.chmod(0o600)
    except OSError:
        pass
    return True


def enforce() -> list[str]:
    """Expire any managed key past its TTL.

    Call after the .env has been loaded into os.environ. Newly seen keys are
    stamped; rotated keys restart the clock; overdue keys are scrubbed from
    both the .env file and the live environment.

    Returns the names of keys expired during this call.
    """
    meta = _read_meta()
    expired: list[str] = []
    dirty = False

    for var in MANAGED_KEYS:
        value = os.environ.get(var, "").strip()
        if not value:
            continue

        entry = meta.get(var)
        fingerprint = _fingerprint(value)

        # First sighting, or the key was rotated by hand -> start the clock.
        if not entry or entry.get("fingerprint") != fingerprint:
            meta[var] = {"set_at": _today().isoformat(), "fingerprint": fingerprint}
            dirty = True
            continue

        try:
            set_at = datetime.fromisoformat(entry["set_at"]).date()
        except (KeyError, ValueError):
            meta[var] = {"set_at": _today().isoformat(), "fingerprint": fingerprint}
            dirty = True
            continue

        if _today() - set_at >= timedelta(days=ttl_days()):
            scrub_from_env_file(var)
            os.environ.pop(var, None)
            meta.pop(var, None)
            dirty = True
            expired.append(var)

    if dirty:
        _write_meta(meta)

    return expired


def status_line(var: str) -> str:
    """Human-readable expiry state for `doctor`."""
    if not os.environ.get(var):
        return "not set"
    remaining = days_remaining(var)
    if remaining is None:
        return "clock starts on next run"
    if remaining < 0:
        return f"expired {abs(remaining)}d ago"
    if remaining == 0:
        return "expires today"
    return f"{remaining}d left"
