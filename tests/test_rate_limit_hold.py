"""Tests for the persisted rate-limit hold store (rate_limit_hold.py)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.rate_limit_hold import (
    HOLDS_FILENAME,
    HOLDS_LOCK_FILENAME,
    LOCK_TIMEOUT_S,
    REPEAT_DECAY_S,
    RETRY_AFTER_MARGIN_S,
    active_holds,
    record_hold,
)

NOW = 1_800_000_000.0
BASE = 120.0
MAX = 1800.0


def _record(tmp_path: Path, slot="3", retry_after=None, now=NOW, base=BASE, max_s=MAX):
    return record_hold(tmp_path, slot, retry_after, now, base, max_s)


def _file(tmp_path: Path) -> Path:
    return tmp_path / HOLDS_FILENAME


def _raw(tmp_path: Path) -> dict:
    return json.loads(_file(tmp_path).read_text())


class TestDuration:
    @pytest.mark.parametrize(
        ("retry_after", "expected"),
        [
            (1, BASE),
            (30.5, BASE),
            (114, BASE),
            (116, 116 + RETRY_AFTER_MARGIN_S),
            (600, 605.0),
            (1795, MAX),
            (1796, MAX),
            (10**6, MAX),
            (1e99, MAX),
            (10**400, MAX),
        ],
    )
    def test_usable_retry_after_adds_margin_and_clamps(
        self, tmp_path, retry_after, expected
    ):
        until = _record(tmp_path, retry_after=retry_after)
        assert until == NOW + expected

    @pytest.mark.parametrize(
        "retry_after",
        [None, 0, 0.0, -5, -0.1, float("nan"), float("inf"), float("-inf"),
         "abc", "30", "Wed, 21 Oct 2026 07:28:00 GMT", True, [30]],
    )
    def test_unusable_retry_after_counts_as_absent(self, tmp_path, retry_after):
        until = _record(tmp_path, retry_after=retry_after)
        assert until == NOW + BASE

    def test_backoff_doubles_per_repeat_after_expiry(self, tmp_path):
        now = NOW
        durations = []
        for _ in range(7):
            until = _record(tmp_path, now=now)
            durations.append(until - now)
            now = until + 1
        assert durations == [120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0]

    def test_backoff_is_capped_by_max(self, tmp_path):
        assert _record(tmp_path, base=3600.0, max_s=60.0) == NOW + 60.0

    def test_retry_after_still_counts_toward_repeat(self, tmp_path):
        first = _record(tmp_path, retry_after=10)
        second = _record(tmp_path, now=first + 1)
        assert second - (first + 1) == BASE * 2
        assert _raw(tmp_path)["holds"]["3"]["count"] == 2

    def test_repeats_lengthen_a_hold_even_with_a_short_retry_after(self, tmp_path):
        now = NOW
        durations = []
        for _ in range(4):
            until = _record(tmp_path, now=now, retry_after=10)
            durations.append(until - now)
            now = until + 1
        assert durations == [120.0, 240.0, 480.0, 960.0]

    def test_a_longer_retry_after_beats_the_backoff(self, tmp_path):
        first = _record(tmp_path, retry_after=10)
        second_now = first + 1
        second = _record(tmp_path, now=second_now, retry_after=400)
        assert second - second_now == 405.0

    def test_huge_stored_count_cannot_overflow(self, tmp_path):
        _file(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "holds": {"3": {"until": NOW - 1, "count": 10**6, "lastAt": NOW - 2}},
        }))
        assert _record(tmp_path) == NOW + MAX

    def test_slots_are_independent(self, tmp_path):
        a = _record(tmp_path, slot="1")
        b = _record(tmp_path, slot="2", retry_after=40)
        assert a == NOW + BASE
        assert b == NOW + BASE
        assert active_holds(tmp_path, NOW) == {"1": a, "2": b}


class TestActiveHoldIsStable:
    def test_second_429_during_hold_neither_doubles_nor_shortens(self, tmp_path):
        until = _record(tmp_path)
        again = _record(tmp_path, now=NOW + 30)
        assert again == until
        entry = _raw(tmp_path)["holds"]["3"]
        assert entry["count"] == 1
        assert entry["until"] == until
        assert entry["lastAt"] == NOW + 30

    def test_shorter_retry_after_does_not_shorten_active_hold(self, tmp_path):
        until = _record(tmp_path, retry_after=500)
        assert _record(tmp_path, retry_after=1, now=NOW + 10) == until

    def test_longer_retry_after_does_not_extend_active_hold(self, tmp_path):
        until = _record(tmp_path)
        assert _record(tmp_path, retry_after=1000, now=NOW + 10) == until

    def test_hold_is_expired_at_exact_until(self, tmp_path):
        until = _record(tmp_path)
        assert active_holds(tmp_path, until - 0.001) == {"3": until}
        assert active_holds(tmp_path, until) == {}
        again = _record(tmp_path, now=until)
        assert again == until + BASE * 2

    def test_count_resets_after_decay_window(self, tmp_path):
        first = _record(tmp_path)
        later = first + REPEAT_DECAY_S + 1
        assert _record(tmp_path, now=later) == later + BASE
        assert _raw(tmp_path)["holds"]["3"]["count"] == 1

    def test_count_survives_inside_decay_window(self, tmp_path):
        first = _record(tmp_path)
        later = NOW + REPEAT_DECAY_S
        assert later > first
        assert _record(tmp_path, now=later) == later + BASE * 2

    def test_refresh_extends_the_repeat_window(self, tmp_path):
        first = _record(tmp_path, retry_after=1000)
        _record(tmp_path, now=first - 1)
        later = first - 1 + REPEAT_DECAY_S - 1
        assert _record(tmp_path, now=later) == later + BASE * 2


class TestActiveHolds:
    def test_missing_file_is_no_holds(self, tmp_path):
        assert active_holds(tmp_path, NOW) == {}
        assert not _file(tmp_path).exists()

    def test_reads_without_the_lock(self, tmp_path):
        until = _record(tmp_path)
        with FileLock(tmp_path / HOLDS_LOCK_FILENAME, timeout=1):
            assert active_holds(tmp_path, NOW) == {"3": until}

    def test_expired_holds_are_filtered(self, tmp_path):
        _record(tmp_path, slot="1", retry_after=10)
        live = _record(tmp_path, slot="2", retry_after=500)
        assert active_holds(tmp_path, NOW + 130) == {"2": live}

    def test_persists_for_another_reader(self, tmp_path):
        until = _record(tmp_path)
        assert active_holds(Path(str(tmp_path)), NOW + 1) == {"3": until}


class TestPruning:
    def test_old_expired_entries_are_pruned_on_write(self, tmp_path):
        _file(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "holds": {
                "1": {"until": NOW - 5000, "count": 3, "lastAt": NOW - 5100},
                "2": {"until": NOW - 10, "count": 1, "lastAt": NOW - 20},
            },
        }))
        _record(tmp_path, slot="3")
        assert set(_raw(tmp_path)["holds"]) == {"2", "3"}

    def test_long_active_hold_is_not_pruned_by_age(self, tmp_path):
        until = _record(tmp_path, slot="1", retry_after=10**5, max_s=86400.0)
        _record(tmp_path, slot="2", now=NOW + REPEAT_DECAY_S + 10, max_s=86400.0)
        assert "1" in _raw(tmp_path)["holds"]
        assert active_holds(tmp_path, NOW + REPEAT_DECAY_S + 10)["1"] == until


class TestCorruptFile:
    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "",
            "[]",
            "null",
            '{"schemaVersion": 2, "holds": {}}',
            '{"holds": {}}',
            '{"schemaVersion": 1, "holds": []}',
            '{"schemaVersion": 1}',
        ],
    )
    def test_corrupt_file_logs_and_yields_no_holds(self, tmp_path, caplog, content):
        _file(tmp_path).write_text(content)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert active_holds(tmp_path, NOW) == {}
        assert any("treating as no holds" in r.getMessage() for r in caplog.records)

    def test_deeply_nested_json_yields_no_holds(self, tmp_path, caplog):
        _file(tmp_path).write_text("[" * 200000 + "]" * 200000)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert active_holds(tmp_path, NOW) == {}
        assert any("treating as no holds" in r.getMessage() for r in caplog.records)

    def test_oversized_integer_literal_yields_no_holds(self, tmp_path, caplog):
        _file(tmp_path).write_text(
            '{"schemaVersion": 1, "holds": {"3": {"until": '
            + "9" * 6000 + ', "count": 1, "lastAt": 1}}}'
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert active_holds(tmp_path, NOW) == {}
        assert any("treating as no holds" in r.getMessage() for r in caplog.records)

    def test_invalid_utf8_yields_no_holds(self, tmp_path, caplog):
        _file(tmp_path).write_bytes(b"\xff\xfe\x00")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert active_holds(tmp_path, NOW) == {}
        assert caplog.records

    def test_corrupt_file_is_replaced_on_next_write(self, tmp_path):
        _file(tmp_path).write_text("{not json")
        until = _record(tmp_path)
        assert _raw(tmp_path) == {
            "schemaVersion": 1,
            "holds": {"3": {"until": until, "count": 1, "lastAt": NOW}},
        }

    @pytest.mark.parametrize(
        "entry",
        [
            None,
            "x",
            [],
            {},
            {"until": "soon", "count": 1, "lastAt": NOW},
            {"until": NOW + 50, "count": 0, "lastAt": NOW},
            {"until": NOW + 50, "count": -1, "lastAt": NOW},
            {"until": NOW + 50, "count": 1.5, "lastAt": NOW},
            {"until": NOW + 50, "count": True, "lastAt": NOW},
            {"until": NOW + 50, "count": "2", "lastAt": NOW},
            {"until": NOW + 50, "count": 1},
            {"until": NOW + 50, "lastAt": NOW},
            {"until": None, "count": 1, "lastAt": NOW},
            {"until": True, "count": 1, "lastAt": NOW},
            {"until": float("nan"), "count": 1, "lastAt": NOW},
            {"until": float("inf"), "count": 1, "lastAt": NOW},
            {"until": NOW + 50, "count": 1, "lastAt": float("nan")},
            {"until": 1e99, "count": 1, "lastAt": NOW},
            {"until": 10**400, "count": 1, "lastAt": NOW},
            {"until": NOW + 50, "count": 1, "lastAt": 10**400},
            {"until": NOW + 10**6, "count": 1, "lastAt": NOW},
            {"until": NOW + 100000, "count": 1, "lastAt": NOW + 86401},
        ],
    )
    def test_malformed_entries_are_dropped_and_good_ones_kept(
        self, tmp_path, caplog, entry
    ):
        good = {"until": NOW + 50, "count": 1, "lastAt": NOW}
        text = json.dumps(
            {"schemaVersion": 1, "holds": {"7": entry, "8": good}},
            allow_nan=True,
        )
        _file(tmp_path).write_text(text)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert active_holds(tmp_path, NOW) == {"8": NOW + 50}
        assert any("malformed hold entry" in r.getMessage() for r in caplog.records)

    def test_malformed_entry_is_dropped_on_next_write(self, tmp_path):
        _file(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "holds": {"7": {"until": "x"}},
        }))
        _record(tmp_path)
        assert set(_raw(tmp_path)["holds"]) == {"3"}

    def test_empty_slot_key_is_dropped(self, tmp_path):
        _file(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "holds": {"": {"until": NOW + 50, "count": 1, "lastAt": NOW}},
        }))
        assert active_holds(tmp_path, NOW) == {}


class TestFileSystem:
    def test_creates_missing_backup_dir(self, tmp_path):
        target = tmp_path / "nested" / "backup"
        until = record_hold(target, "3", None, NOW, BASE, MAX)
        assert active_holds(target, NOW) == {"3": until}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_file_is_private(self, tmp_path):
        _record(tmp_path)
        assert _file(tmp_path).stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
    def test_symlinked_file_is_written_through(self, tmp_path):
        real_dir = tmp_path / "dotfiles"
        real_dir.mkdir()
        real = real_dir / "holds.json"
        real.write_text(json.dumps({"schemaVersion": 1, "holds": {}}))
        backup = tmp_path / "backup"
        backup.mkdir()
        link = backup / HOLDS_FILENAME
        os.symlink(real, link)
        until = record_hold(backup, "3", None, NOW, BASE, MAX)
        assert link.is_symlink()
        assert os.readlink(link) == str(real)
        assert json.loads(real.read_text())["holds"]["3"]["until"] == until
        assert active_holds(backup, NOW) == {"3": until}

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
    def test_dangling_symlink_writes_where_it_points(self, tmp_path):
        real = tmp_path / "elsewhere.json"
        backup = tmp_path / "backup"
        backup.mkdir()
        link = backup / HOLDS_FILENAME
        os.symlink(real, link)
        until = record_hold(backup, "3", None, NOW, BASE, MAX)
        assert link.is_symlink()
        assert json.loads(real.read_text())["holds"]["3"]["until"] == until


class TestLocking:
    def test_lock_timeout_raises_lock_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.rate_limit_hold.LOCK_TIMEOUT_S", 0.2)
        holder = FileLock(tmp_path / HOLDS_LOCK_FILENAME, timeout=1)
        assert holder.acquire()
        try:
            with pytest.raises(LockError):
                _record(tmp_path)
        finally:
            holder.release()
        assert not _file(tmp_path).exists()

    def test_lock_timeout_constant_is_two_seconds(self):
        assert LOCK_TIMEOUT_S == 2.0

    def test_threads_on_one_slot_keep_one_entry(self, tmp_path):
        results: list[float] = []
        errors: list[BaseException] = []

        def worker():
            try:
                results.append(_record(tmp_path))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(set(results)) == 1
        entry = _raw(tmp_path)["holds"]["3"]
        assert entry["count"] == 1
        assert entry["until"] == results[0]

    def test_two_processes_lose_no_entry(self, tmp_path):
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from claude_swap.rate_limit_hold import record_hold\n"
            "backup, tag = Path(sys.argv[1]), sys.argv[2]\n"
            "for i in range(15):\n"
            "    record_hold(backup, f'{tag}{i}', None, 1800000000.0, 120.0, 1800.0)\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), tag],
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in ("a", "b")
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
        expected = {f"{tag}{i}" for tag in ("a", "b") for i in range(15)}
        assert set(_raw(tmp_path)["holds"]) == expected
        assert set(active_holds(tmp_path, NOW)) == expected
