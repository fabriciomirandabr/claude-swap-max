"""Persisted per-slot holds for accounts that answered a 429 on ``/v1/messages``.

A hold is the window during which a slot is out of every ranking. State lives
in ``<backup_dir>/rate_limit_holds.json`` so holds survive a daemon restart
and are shared by the proxy and the auto-switch engine::

    {"schemaVersion": 1, "holds": {"3": {"until": 1793.., "count": 2, "lastAt": 1793..}}}

Readers are lock-free (the file is replaced atomically). Writers take a
``FileLock`` so two processes never lose each other's entry.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

RETRY_AFTER_MARGIN_S = 5.0
REPEAT_DECAY_S = 3600.0
LOCK_TIMEOUT_S = 2.0

HOLDS_FILENAME = "rate_limit_holds.json"
HOLDS_LOCK_FILENAME = "rate_limit_holds.lock"
SCHEMA_VERSION = 1

_MAX_HOLD_SPAN_S = 86400.0
_MAX_BACKOFF_EXPONENT = 30

_logger = logging.getLogger("claude-swap")


def _holds_path(backup_dir: Path) -> Path:
    return backup_dir / HOLDS_FILENAME


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value) -> bool:
    if not _is_number(value):
        return False
    return isinstance(value, int) or math.isfinite(value)


def _parse_entry(slot, entry, now: float) -> dict | None:
    if not isinstance(slot, str) or not slot or not isinstance(entry, dict):
        return None
    until = entry.get("until")
    last_at = entry.get("lastAt")
    count = entry.get("count")
    if not _is_number(until) or not _is_number(last_at):
        return None
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    try:
        until = float(until)
        last_at = float(last_at)
    except OverflowError:
        return None
    if not math.isfinite(until) or not math.isfinite(last_at):
        return None
    if until - last_at > _MAX_HOLD_SPAN_S:
        return None
    if last_at > now + _MAX_HOLD_SPAN_S:
        return None
    return {"until": until, "count": count, "lastAt": last_at}


def _load(backup_dir: Path, now: float) -> dict[str, dict]:
    path = _holds_path(backup_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as e:
        _logger.warning("Could not read %s (%s); treating as no holds", path, e)
        return {}
    if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
        _logger.warning("%s has an unsupported shape; treating as no holds", path)
        return {}
    holds = raw.get("holds")
    if not isinstance(holds, dict):
        _logger.warning("%s has no holds object; treating as no holds", path)
        return {}
    parsed: dict[str, dict] = {}
    for slot, entry in holds.items():
        clean = _parse_entry(slot, entry, now)
        if clean is None:
            _logger.warning("%s: dropping malformed hold entry for slot %r", path, slot)
            continue
        parsed[slot] = clean
    return parsed


def active_holds(backup_dir: Path, now: float) -> dict[str, float]:
    """Slot -> hold expiry for every hold with ``until > now``; lock-free."""
    return {
        slot: entry["until"]
        for slot, entry in _load(backup_dir, now).items()
        if entry["until"] > now
    }


def _valid_retry_after(retry_after) -> float | None:
    if retry_after is None or not _finite(retry_after) or retry_after <= 0:
        return None
    try:
        return float(retry_after)
    except OverflowError:
        return math.inf


def record_hold(
    backup_dir: Path,
    slot: str,
    retry_after: float | None,
    now: float,
    base_s: float,
    max_s: float,
) -> float:
    """Hold ``slot`` after a 429 and return the hold expiry.

    The duration is ``min(max_s, base_s * 2 ** (n - 1))`` with ``n`` the repeat
    count (reset to 1 after ``REPEAT_DECAY_S`` without a 429); a finite
    ``retry_after`` > 0 raises that floor to ``retry_after + 5`` when larger,
    so repeats lengthen a hold even with a short Retry-After. A 429 during an
    active hold only refreshes ``lastAt``: it never extends or shortens it.
    Raises ``LockError`` if the lock is not acquired within ``LOCK_TIMEOUT_S``.
    """
    path = _holds_path(backup_dir)
    with FileLock(backup_dir / HOLDS_LOCK_FILENAME, timeout=LOCK_TIMEOUT_S):
        holds = _load(backup_dir, now)
        previous = holds.get(slot)
        if previous is not None and previous["until"] > now:
            previous["lastAt"] = now
            until = previous["until"]
        else:
            if previous is not None and now - previous["lastAt"] <= REPEAT_DECAY_S:
                count = previous["count"] + 1
            else:
                count = 1
            valid = _valid_retry_after(retry_after)
            exponent = min(count - 1, _MAX_BACKOFF_EXPONENT)
            duration = base_s * 2**exponent
            if valid is not None:
                duration = max(valid + RETRY_AFTER_MARGIN_S, duration)
            duration = min(max_s, duration)
            until = now + duration
            holds[slot] = {"until": until, "count": count, "lastAt": now}
        kept = {
            key: entry
            for key, entry in holds.items()
            if entry["until"] > now or now - entry["lastAt"] <= REPEAT_DECAY_S
        }
        atomic_write_json(path, {"schemaVersion": SCHEMA_VERSION, "holds": kept})
    return until
