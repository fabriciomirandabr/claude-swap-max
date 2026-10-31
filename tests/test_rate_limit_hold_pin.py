"""R26 and the real-switcher rows of rate-limit-hold.

The proxy in ``vendor/cswap-pin`` and the host switcher version separately, and
every earlier proxy test drove a fake host. A fake that accepts a keyword the
real ``switch()`` does not is how the at-limit switch raised ``TypeError`` on
every 429 while the proxy tests stayed green. These tests drive the REAL
``ClaudeAccountSwitcher`` in a temp home, with only the network seams stubbed
(the liveness probe, the usage fetch) and the relay's wait patched out.

They must run against the working-tree proxy, not the copy installed in the
virtualenv: the root ``pyproject.toml`` puts ``vendor/cswap-pin/src`` on
``pythonpath``, so a plain ``uv run pytest`` loads it. The ``pp`` fixture fails
loudly when the loaded ``cswap_pin.proxy`` is any other file.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from claude_swap import oauth, rate_limit_hold
from claude_swap.models import Platform
from claude_swap.paths import get_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord, UsageStore

VENDOR_SRC = Path(__file__).resolve().parents[1] / "vendor" / "cswap-pin" / "src"

RESET_1 = b"9999999991"
RESET_2 = b"9999999992"
RESET_3 = b"9999999993"


@pytest.fixture
def pp(monkeypatch):
    """The working-tree ``cswap_pin.proxy`` with every wall/hold memo reset."""
    from cswap_pin import proxy

    loaded = Path(proxy.__file__).resolve()
    if VENDOR_SRC not in loaded.parents:
        pytest.fail(
            f"cswap_pin.proxy was loaded from {loaded}, not the working tree; "
            f"pythonpath in pyproject.toml must include {VENDOR_SRC}"
        )
    for memo in (
        proxy._walled_switch_seen,
        proxy._walled_slots,
        proxy._walled_switch_seen_by_session,
        proxy._walled_headroom_seen,
        proxy._walled_conversions_by_session,
        proxy._walled_relay_spell_by_session,
        proxy._usage_header_seen,
        proxy._usage_header_spawn_seen,
    ):
        memo.clear()
    monkeypatch.setattr(proxy, "_fleet_held_until", 0.0)
    lines: list[str] = []
    monkeypatch.setattr(proxy, "_log_lifecycle", lines.append)
    waits: list[float] = []
    holds: list[float] = []

    def record_wait(seconds):
        waits.append(seconds)
        return proxy._fleet_held_until

    proxy._test_real_hold_reply = proxy._hold_reply
    proxy._test_real_hold_relay_wait = proxy._hold_relay_wait
    monkeypatch.setattr(proxy, "_hold_relay_wait", record_wait)
    monkeypatch.setattr(proxy, "_hold_reply", holds.append)
    proxy._test_lines = lines
    proxy._test_waits = waits
    proxy._test_holds = holds
    yield proxy
    for memo in (
        proxy._walled_switch_seen,
        proxy._walled_slots,
        proxy._walled_switch_seen_by_session,
        proxy._walled_headroom_seen,
        proxy._walled_conversions_by_session,
        proxy._walled_relay_spell_by_session,
    ):
        memo.clear()


def _usage(pct: float) -> dict:
    return {
        "five_hour": {"pct": pct, "clock": "Jan 1 03:00", "countdown": "1h"},
        "seven_day": {"pct": 0.0, "clock": "Jan 2 03:00", "countdown": "2d"},
    }


class Fleet:
    """A real switcher fleet in a temp home plus the stubbed network seams."""

    def __init__(self, home: Path, monkeypatch, count: int, usage: dict):
        self.home = home
        self.usage = usage
        self.probed: list[str] = []
        self.verdicts: dict[str, bool | None] = {}
        self.switch_calls: list[dict] = []
        self.switch_results: list[dict] = []
        self.delay_s = 0.0
        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.LINUX)
        )
        self.switcher = ClaudeAccountSwitcher()
        self.switcher.platform = Platform.LINUX
        self.switcher._setup_directories()
        self.switcher._init_sequence_file()
        self.backup = get_backup_root()
        self.emails = {}
        for num in range(1, count + 1):
            self._seed(num, f"{chr(96 + num)}@example.com")
        self.make_live(1)
        self._seed_store()

        monkeypatch.setattr(
            ClaudeAccountSwitcher,
            "_usage_by_account",
            lambda inner: {k: v for k, v in self.usage.items()},
        )
        monkeypatch.setattr(ClaudeAccountSwitcher, "list_accounts", lambda *a, **k: None)
        monkeypatch.setattr(
            oauth, "probe_oauth_profile_live", self._probe
        )
        monkeypatch.setattr(
            oauth,
            "try_fetch_usage_for_account",
            lambda *a, **k: (_ for _ in ()).throw(OSError("blocked in test")),
        )

        original = ClaudeAccountSwitcher.switch
        fleet = self

        @functools.wraps(original)
        def spy(inner, *args, **kwargs):
            fleet.switch_calls.append(dict(kwargs))
            if fleet.delay_s:
                time.sleep(fleet.delay_s)
            result = original(inner, *args, **kwargs)
            fleet.switch_results.append(result)
            return result

        monkeypatch.setattr(ClaudeAccountSwitcher, "switch", spy)

    def _probe(self, token, timeout_s=5.0):
        self.probed.append(token)
        return self.verdicts.get(token, True)

    def _seed(self, num: int, email: str) -> None:
        s = self.switcher
        self.emails[num] = email
        s._write_account_credentials(
            str(num),
            email,
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps({"oauthAccount": {
                "emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _seed_store(self) -> None:
        records = {
            num: FetchRecord(usage=reading)
            for num, reading in self.usage.items()
            if isinstance(reading, dict)
        }
        UsageStore(self.backup / "cache").record(
            records,
            {num: (self.emails[int(num)], "") for num in records},
        )

    def make_live(self, num: int) -> None:
        (self.home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live"}}))
        (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": self.emails[num],
                "accountUuid": f"uuid-{num}"}}))

    def make_unmanaged(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "stranger@example.com",
                "accountUuid": "uuid-stranger"}}))

    def active(self) -> int:
        return self.switcher._get_sequence_data()["activeAccountNumber"]

    def live_slot(self) -> str | None:
        return ClaudeAccountSwitcher().current_account_number()

    def holds(self) -> dict[str, float]:
        return rate_limit_hold.active_holds(self.backup, time.time())

    def hold_file(self) -> dict:
        path = self.backup / rate_limit_hold.HOLDS_FILENAME
        return json.loads(path.read_text()) if path.exists() else {}

    def hold_now(self, slot: str, retry_after: float | None = 600) -> float:
        return rate_limit_hold.record_hold(
            self.backup, slot, retry_after, time.time(), 120, 1800
        )

    def disable(self, num: int) -> None:
        self.switcher.set_account_disabled(str(num), True)


@pytest.fixture
def fleet(temp_home, monkeypatch):
    def build(count=3, usage=None):
        reading = usage if usage is not None else {
            "1": _usage(10), "2": _usage(50), "3": _usage(20)}
        return Fleet(temp_home, monkeypatch, count, reading)

    return build


def _relay(pp, reset=RESET_1, retry_after=b"90", auth="Bearer sk-live",
           session="s1") -> bytes:
    up_a, up_b = socket.socketpair()
    cl_a, cl_b = socket.socketpair()
    try:
        head = (
            b"HTTP/1.1 429 Too Many Requests\r\n"
            b"anthropic-ratelimit-unified-reset: " + reset + b"\r\n"
            b"retry-after: " + retry_after + b"\r\n"
            b"anthropic-ratelimit-unified-status: rejected\r\n"
            b"x-should-retry: true\r\n"
            b"Content-Length: 2\r\n\r\nno"
        )
        up_b.sendall(head)
        up_b.shutdown(socket.SHUT_WR)
        pp._relay_response(
            up_a, cl_a, 0, method="POST", path="/v1/messages",
            auth=auth, session=session,
        )
        cl_a.shutdown(socket.SHUT_WR)
        return cl_b.recv(4096)
    finally:
        for sock in (up_a, up_b, cl_a, cl_b):
            try:
                sock.close()
            except OSError:
                pass


def _switch_off(pp, reset=RESET_1, retry_after=b"90", auth="Bearer sk-live",
                session="s1") -> bool:
    return pp._switch_off_walled_account(
        reset, retry_after, auth=auth, session=session)


class TestTheLoadedPinIsTheWorkingTree:
    def test_proxy_comes_from_the_vendored_source(self, pp):
        print(f"cswap_pin.proxy.__file__ = {pp.__file__}")
        assert VENDOR_SRC in Path(pp.__file__).resolve().parents
        assert hasattr(pp, "_hold_relay_wait")
        assert hasattr(pp, "_held_slots")


def _walled_usage(resets_in_s: float) -> dict:
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(
        time.time() + resets_in_s, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "five_hour": {"pct": 100.0, "resets_at": iso},
        "seven_day": {"pct": 0.0, "resets_at": iso},
    }


class TestTheProxyReadsTheRealUsageEntry:
    def test_live_account_headroom_is_a_real_number_from_a_stored_reading(
        self, pp, fleet,
    ):
        fleet(usage={"1": _usage(10), "2": _usage(50), "3": _usage(20)})
        assert pp._live_account_headroom("1") == 90.0
        assert pp._live_account_headroom("2") == 50.0

    def test_fleet_earliest_provable_reset_is_the_real_earliest_reset(
        self, pp, fleet,
    ):
        from datetime import datetime

        f = fleet(usage={
            "1": _walled_usage(3600),
            "2": _walled_usage(7200),
            "3": _usage(10),
        })
        earliest = pp._fleet_earliest_provable_reset()
        entry = ClaudeAccountSwitcher().usage_entries_by_account(fetch=set())["1"]
        stored = entry.decision_value()["five_hour"]["resets_at"]
        assert earliest == datetime.fromisoformat(
            stored.replace("Z", "+00:00")).timestamp()
        assert abs(earliest - (time.time() + 3600)) < 5
        assert f.active() == 1


class TestR26TheProxyKwargsBindToTheRealSwitch:
    def test_the_real_host_takes_exclude(self, pp, temp_home):
        assert pp._switch_takes_exclude() is True

    def test_a_host_switch_without_exclude_is_detected(self, pp, monkeypatch):
        def old_switch(self, strategy=None, json_output=False, models=(),
                       model_source=None, *, current_at_limit=False):
            raise AssertionError("never called")

        monkeypatch.setattr(ClaudeAccountSwitcher, "switch", old_switch)
        assert pp._switch_takes_exclude() is False

    def test_the_kwargs_the_off_path_really_passes_bind_to_the_real_signature(
        self, pp, fleet,
    ):
        real_signature = inspect.signature(ClaudeAccountSwitcher.switch)
        f = fleet()
        converted = _switch_off(pp)
        assert len(f.switch_calls) == 1
        kwargs = f.switch_calls[0]
        bound = real_signature.bind(None, **kwargs)
        assert converted is True
        assert bound.arguments["current_at_limit"] is True
        assert kwargs["exclude"] == frozenset({"1"})
        assert set(kwargs) <= set(real_signature.parameters)
        assert kwargs["models"] == ("all",)
        assert kwargs["strategy"] == "best"
        assert kwargs["json_output"] is True

    def test_every_keyword_the_proxy_passes_is_keyword_only_or_named(self, pp, fleet):
        params = inspect.signature(ClaudeAccountSwitcher.switch).parameters
        f = fleet()
        _switch_off(pp)
        for name in f.switch_calls[0]:
            assert name in params, name
        assert params["current_at_limit"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["exclude"].kind is inspect.Parameter.KEYWORD_ONLY


class TestRealSwitcherRows:
    def test_a_429_on_the_live_slot_switches_validates_and_holds(self, pp, fleet):
        f = fleet()
        t0 = time.time()
        assert _switch_off(pp) is True
        result = f.switch_results[0]
        assert result["switched"] is True
        assert result["validated"] is True
        assert f.active() == 3
        assert f.probed == ["sk-3"]
        on_disk = f.hold_file()["holds"]["1"]
        assert abs(on_disk["until"] - (t0 + 120)) < 5, on_disk
        assert on_disk["count"] == 1
        assert f.holds()["1"] == on_disk["until"]
        assert pp._fleet_held_until == 0.0
        assert any("walled account switched off, relaying a 401" in line
                   for line in pp._test_lines), pp._test_lines

    def test_the_reply_is_a_401_without_any_rate_limit_header(self, pp, fleet):
        f = fleet()
        got = _relay(pp)
        assert got.startswith(b"HTTP/1.1 401"), got[:60]
        lowered = got.lower()
        assert b"retry-after" not in lowered
        assert b"anthropic-ratelimit" not in lowered
        assert b"x-should-retry" not in lowered
        assert f.active() == 3

    def test_a_second_429_moves_on_excluding_the_first(self, pp, fleet):
        f = fleet()
        assert _switch_off(pp, reset=RESET_1) is True
        assert f.active() == 3
        assert _switch_off(pp, reset=RESET_2, auth="Bearer sk-3") is True
        assert f.active() == 2
        assert [c["exclude"] for c in f.switch_calls] == [
            frozenset({"1"}), frozenset({"1", "3"})]
        assert f.probed == ["sk-3", "sk-2"]
        assert set(f.holds()) == {"1", "3"}

    def test_no_slot_is_revisited_during_its_hold(self, pp, fleet):
        f = fleet()
        _switch_off(pp, reset=RESET_1)
        _switch_off(pp, reset=RESET_2, auth="Bearer sk-3")
        _switch_off(pp, reset=RESET_3, auth="Bearer sk-2")
        landed = [r["to"]["number"] for r in f.switch_results if r.get("switched")]
        assert landed == [3, 2]
        assert f.active() == 2
        assert f.switch_results[2]["reason"] == "candidates-exhausted"

    def test_every_slot_held_returns_false_and_marks_the_fleet_held(self, pp, fleet):
        f = fleet()
        f.hold_now("2")
        f.hold_now("3")
        now = time.time()
        assert _switch_off(pp) is False
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        assert f.active() == 1
        assert pp._fleet_held_until > now, pp._test_lines

    def test_hold_only_detection_reads_the_real_usage_entry_without_raising(
        self, pp, fleet,
    ):
        f = fleet()
        f.hold_now("2")
        f.hold_now("3")
        _switch_off(pp)
        assert not any("could not tell hold-only slots" in line
                       for line in pp._test_lines), pp._test_lines
        entries = ClaudeAccountSwitcher().usage_entries_by_account(fetch=set())
        for entry in entries.values():
            assert isinstance(entry.decision_value(), dict)

    def test_every_slot_held_relays_a_stripped_429_that_waits_then_retries_soon(
        self, pp, fleet,
    ):
        f = fleet()
        f.hold_now("2")
        f.hold_now("3")
        got = _relay(pp)
        assert got.startswith(b"HTTP/1.1 429"), got[:60]
        lowered = got.lower()
        assert b"representative-claim" not in lowered
        assert b"anthropic-ratelimit" not in lowered
        assert b"x-should-retry" not in lowered
        found = re.findall(rb"(?im)^retry-after: (\d+)\r$", got)
        assert len(found) == 1 and 1 <= int(found[0]) <= 60, got
        assert len(pp._test_waits) == 1
        assert 0 < pp._test_waits[0] <= 120
        assert got.endswith(b"no")
        assert f.active() == 1

    def test_a_dead_first_choice_falls_to_the_next_in_the_same_call(self, pp, fleet):
        f = fleet()
        f.verdicts["sk-3"] = False
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "consume_backup_grant",
                lambda self, *a, **k: oauth.RefreshOutcome(None, "invalid_grant"),
            )
            assert _switch_off(pp) is True
        assert f.switch_results[0]["validated"] is True
        assert f.active() == 2
        assert f.probed == ["sk-3", "sk-2"]

    def test_a_landing_with_no_verdict_converts_to_a_401(self, pp, fleet):
        f = fleet()
        f.verdicts["sk-3"] = None
        got = _relay(pp)
        assert f.switch_results[0]["switched"] is True
        assert "validated" not in f.switch_results[0]
        assert f.active() == 3
        assert got.startswith(b"HTTP/1.1 401"), got[:60]
        assert b"retry-after" not in got.lower()
        assert any("no validation verdict" in line
                   for line in pp._test_lines), pp._test_lines

    def test_a_target_the_probe_proves_dead_is_never_landed_on(self, pp, fleet):
        f = fleet(count=2, usage={"1": _usage(10), "2": _usage(50)})
        f.verdicts["sk-2"] = False
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "consume_backup_grant",
                lambda self, *a, **k: oauth.RefreshOutcome(None, "invalid_grant"),
            )
            got = _relay(pp)
        assert f.active() == 1
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        assert got.startswith(b"HTTP/1.1 429"), got[:60]


class TestEdgeRows:
    def test_one_account_is_held_but_the_host_answers_only_one_account(
        self, pp, fleet,
    ):
        f = fleet(count=1, usage={"1": _usage(10)})
        got = _relay(pp)
        assert f.switch_results[0]["reason"] == "only-one-account"
        assert f.active() == 1
        assert set(f.holds()) == {"1"}
        assert pp._fleet_held_until == 0.0
        assert got.startswith(b"HTTP/1.1 429"), got[:60]
        assert re.search(rb"(?im)^retry-after: 30\r$", got), got
        assert pp._test_waits == []

    def test_all_peers_disabled_leaves_only_the_held_live_slot_hold_only(
        self, pp, fleet,
    ):
        f = fleet()
        f.disable(2)
        f.disable(3)
        now = time.time()
        assert _switch_off(pp) is False
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        assert f.active() == 1
        assert f.probed == []
        assert pp._fleet_held_until > now

    def test_a_hold_on_a_disabled_slot_does_not_set_the_fleet_expiry(self, pp, fleet):
        f = fleet()
        f.disable(2)
        short = f.hold_now("2", retry_after=1)
        long_hold = f.hold_now("3", retry_after=900)
        assert _switch_off(pp, retry_after=b"600") is False
        live_until = f.holds()["1"]
        assert pp._fleet_held_until == min(live_until, long_hold)
        assert pp._fleet_held_until > short

    def test_a_hold_that_is_expired_at_its_exact_until_is_not_held(self, pp, fleet):
        f = fleet()
        until = f.hold_now("2", retry_after=30)
        with pp._walled_switch_lock:
            assert "2" in pp._held_slots(until - 0.001)
            assert "2" not in pp._held_slots(until)

    def test_a_reset_earlier_than_the_hold_walls_the_slot_only_until_the_reset(
        self, pp, fleet,
    ):
        fleet()
        reset = int(time.time()) + 20
        _switch_off(pp, reset=str(reset).encode(), retry_after=b"90")
        assert pp._walled_slots["1"] == float(reset)

    def test_an_unmanaged_live_login_records_no_hold(self, pp, fleet):
        f = fleet()
        f.make_unmanaged()
        assert f.live_slot() is None
        _switch_off(pp, auth="Bearer sk-live")
        assert f.hold_file() == {}
        assert pp._walled_slots == {}

    def test_a_429_without_a_reset_header_is_relayed_untouched_and_holds_nothing(
        self, pp, fleet,
    ):
        f = fleet()
        assert _switch_off(pp, reset=b"") is False
        assert f.switch_calls == []
        assert f.hold_file() == {}

    @pytest.mark.parametrize(
        "retry_after, seconds",
        [
            (b"abc", 120),
            (b"-5", 120),
            (b"0", 120),
            (b"nan", 120),
            (b"inf", 120),
            (b"-inf", 120),
            (b"", 120),
            (b"Wed, 21 Oct 2026 07:28:00 GMT", 120),
            (b"\xff\xfe", 120),
            (b"1e99", 1800),
            (b"90", 120),
            (b"1", 120),
            (b"150", 155),
        ],
    )
    def test_a_hostile_retry_after_counts_as_absent_or_clamps(
        self, pp, fleet, retry_after, seconds,
    ):
        f = fleet()
        t0 = time.time()
        _switch_off(pp, retry_after=retry_after)
        entry = f.hold_file()["holds"]["1"]
        assert abs((entry["until"] - t0) - seconds) < 5, entry
        assert entry["count"] == 1

    def test_ten_threads_on_one_wall_make_one_switch_one_hold(self, pp, fleet):
        f = fleet()
        f.delay_s = 0.4
        barrier = threading.Barrier(10)
        results: list[bool] = []
        errors: list[BaseException] = []

        def hit(i: int) -> None:
            try:
                barrier.wait(timeout=10)
                results.append(_switch_off(pp, session=f"s{i}"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert len(results) == 10 and all(results), results
        assert len(f.switch_calls) == 1, f.switch_calls
        holds = f.hold_file()["holds"]
        assert set(holds) == {"1"}
        assert holds["1"]["count"] == 1
        assert f.active() == 3


def _fable_usage(five_seven: float, fable: float, resets_in_s: float = 3600) -> dict:
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(
        time.time() + resets_in_s, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "five_hour": {"pct": five_seven, "resets_at": iso},
        "seven_day": {"pct": five_seven, "resets_at": iso},
        "scoped": [{"name": "Fable", "pct": fable, "resets_at": iso}],
    }


class TestStaleBearerOnTheRealHost:
    def test_a_live_slot_with_fable_full_and_5h_7d_low_still_gets_the_401(
        self, pp, fleet,
    ):
        f = fleet(usage={
            "1": _fable_usage(30.0, 100.0), "2": _usage(50), "3": _usage(20)})
        assert pp._live_account_headroom("1") == 70.0
        assert _switch_off(pp, auth="Bearer sk-old") is True
        assert f.switch_calls == []
        assert pp._fleet_held_until == 0.0

    def test_a_reading_missing_a_base_window_yields_no_headroom(self, pp, fleet):
        fleet(usage={
            "1": {"five_hour": {"pct": 10.0}}, "2": _usage(50),
            "3": _usage(20)})
        assert pp._live_account_headroom("1") is None

    def test_a_persisted_hold_on_the_live_slot_sends_the_stale_bearer_to_switch(
        self, pp, fleet,
    ):
        f = fleet()
        f.hold_now("1")
        assert pp._walled_slots == {}
        _switch_off(pp, auth="Bearer sk-old")
        assert len(f.switch_calls) == 1, f.switch_calls
        assert "1" in f.switch_calls[0]["exclude"]

    def test_a_none_slot_re_read_does_not_count_as_moved(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet()
        real_slot = pp._live_account_slot
        reads: list[int] = []

        def slot_then_blind():
            reads.append(1)
            return real_slot() if len(reads) == 1 else None

        monkeypatch.setattr(pp, "_live_account_slot", slot_then_blind)
        _relay(pp, reset=RESET_1)
        assert len(f.switch_calls) == 1, f.switch_calls
        assert set(f.holds()) == {"1"}, f.holds()
        assert "1" in f.switch_calls[0]["exclude"]


class TestExhaustedFleetNeverEndsTheTurnOnTheRealHost:
    @staticmethod
    def _assert_waits_and_strips(pp, got):
        assert got.startswith(b"HTTP/1.1 429"), got[:60]
        lowered = got.lower()
        assert b"representative-claim" not in lowered, got
        assert b"anthropic-ratelimit" not in lowered, got
        assert b"x-should-retry" not in lowered, got
        found = re.findall(rb"(?im)^retry-after: (\d+)\r$", got)
        assert len(found) == 1 and 1 <= int(found[0]) <= 60, got
        assert len(pp._test_waits) == 1, pp._test_waits
        assert 0 < pp._test_waits[0] <= 120, pp._test_waits

    def test_a_peer_at_100_percent_with_a_past_reset(self, pp, fleet):
        f = fleet(usage={
            "1": _walled_usage(-100), "2": _walled_usage(-100),
            "3": _walled_usage(-100)})
        before = time.time()
        got = _relay(pp)
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        self._assert_waits_and_strips(pp, got)
        assert abs(pp._fleet_held_until - (before + 120)) < 5

    def test_peers_with_fable_full_and_5h_7d_low_still_take_the_request(
        self, pp, fleet,
    ):
        f = fleet(usage={
            "1": _usage(30), "2": _fable_usage(30.0, 100.0),
            "3": _fable_usage(30.0, 100.0)})
        got = _relay(pp)
        assert f.switch_results[0]["switched"] is True
        assert f.active() in (2, 3)
        assert got.startswith(b"HTTP/1.1 401"), got[:60]
        assert pp._test_waits == []

    def test_a_peer_whose_reset_is_20_seconds_away(self, pp, fleet):
        f = fleet(usage={
            "1": _walled_usage(20), "2": _walled_usage(20),
            "3": _walled_usage(20)})
        before = time.time()
        got = _relay(pp)
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        self._assert_waits_and_strips(pp, got)
        assert abs(pp._fleet_held_until - (before + 20)) < 5
        assert 15 <= pp._test_waits[0] <= 20

    def test_a_really_walled_fleet_with_far_resets_waits_120_and_asks_for_60(
        self, pp, fleet,
    ):
        f = fleet(usage={
            "1": _walled_usage(3600), "2": _walled_usage(7200),
            "3": _walled_usage(5400)})
        got = _relay(pp)
        assert f.switch_results[0]["reason"] == "candidates-exhausted"
        self._assert_waits_and_strips(pp, got)
        assert pp._test_waits == [120.0]
        assert abs(pp._fleet_held_until - (time.time() + 3600)) < 5
        assert re.search(rb"(?im)^retry-after: 60\r$", got), got


def _decide_across_moves(
    pp, monkeypatch, auth, first_move, second_move=None, reset=RESET_1,
    session="s1",
):
    """Run `_switch_off_walled_account` on a thread that has read its (slot,
    token) pair and then waits for the wall lock, while ``first_move`` runs
    under that lock; ``second_move`` runs between the two passes."""
    pair_read = threading.Event()
    real_token = pp._active_oauth_token
    real_decide = pp._decide_walled_switch

    def token_then_signal():
        token = real_token()
        pair_read.set()
        return token

    def decide(reset_, retry_after, auth_, session_, pair, note=None):
        out = real_decide(reset_, retry_after, auth_, session_, pair, note=note)
        if pair is None and isinstance(out, tuple) and second_move is not None:
            second_move()
        return out

    monkeypatch.setattr(pp, "_active_oauth_token", token_then_signal)
    monkeypatch.setattr(pp, "_decide_walled_switch", decide)
    box: dict = {}
    pp._walled_switch_lock.acquire()
    worker = threading.Thread(
        target=lambda: box.update(
            r=_switch_off(pp, reset=reset, auth=auth, session=session)))
    worker.start()
    try:
        assert pair_read.wait(10), "the worker never read its pair"
        first_move()
    finally:
        pp._walled_switch_lock.release()
    worker.join(30)
    assert not worker.is_alive()
    return box["r"]


class TestTheLiveSlotIsReReadOnBothPasses:
    @staticmethod
    def _to(f, slot, hold=None):
        def move():
            if hold is not None:
                f.hold_now(hold)
            ClaudeAccountSwitcher().switch_to(slot, json_output=True)
        return move

    def test_a_slot_that_moves_again_between_the_passes_flips_nothing(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet(count=4, usage={
            "1": _usage(10), "2": _usage(50), "3": _usage(20), "4": _usage(30)})
        verdict = _decide_across_moves(
            pp, monkeypatch, "Bearer sk-live",
            self._to(f, "2", hold="1"), self._to(f, "3", hold="2"))
        assert verdict is False
        assert f.switch_calls == []
        assert f.active() == 3
        assert set(f.holds()) == {"1", "2"}
        assert pp._fleet_held_until == 0.0

    def test_a_slot_that_moves_again_never_forges_an_exhausted_fleet(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet(count=3)
        monkeypatch.setattr(pp, "_fleet_held_until", time.time() + 500)
        verdict = _decide_across_moves(
            pp, monkeypatch, "Bearer sk-live",
            self._to(f, "2", hold="1"), self._to(f, "3", hold="2"))
        assert verdict is False
        assert f.switch_calls == []
        assert f.active() == 3
        assert pp._fleet_held_until == 0.0
        assert pp._test_waits == []

    def test_a_move_inside_the_bearer_branch_is_caught_before_the_switch(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet()
        f.hold_now("1")
        real_held = pp._held_slots
        moved: list[int] = []

        def held_then_move(now):
            held = real_held(now)
            if not moved:
                moved.append(1)
                ClaudeAccountSwitcher().switch_to("2", json_output=True)
            return held

        monkeypatch.setattr(pp, "_held_slots", held_then_move)
        verdict = _switch_off(pp, auth="Bearer sk-old")
        assert verdict is True
        assert f.switch_calls == []
        assert f.active() == 2

    def test_a_stale_bearer_is_judged_on_the_slot_that_is_live_after_the_lock(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet()
        verdict = _decide_across_moves(
            pp, monkeypatch, "Bearer sk-old", self._to(f, "2"))
        assert verdict is True
        assert f.switch_calls == []
        slots = {key[1] for key in pp._walled_switch_seen_by_session}
        assert slots == {"2"}, slots


class TestConversionsDoNotEatTheClientsRetryBudget:
    RESETS = [str(9999999990 + n).encode() for n in range(1, 9)]

    def _convert(self, pp, n, session="s1"):
        return _relay(
            pp, reset=self.RESETS[n], auth="Bearer sk-old", session=session)

    def test_the_first_two_conversions_are_instant_401s(self, pp, fleet):
        fleet()
        for n in range(2):
            assert self._convert(pp, n).startswith(b"HTTP/1.1 401")
        assert pp._test_holds == []

    def test_from_the_third_conversion_the_401_is_held_before_its_first_byte(
        self, pp, fleet,
    ):
        fleet()
        for n in range(5):
            got = self._convert(pp, n)
            assert got.startswith(b"HTTP/1.1 401"), (n, got[:60])
            assert b"retry-after" not in got.lower()
        assert pp._test_holds == [120.0, 120.0, 120.0]

    def test_from_the_sixth_conversion_the_reply_is_the_waiting_relay_bar_one_401(
        self, pp, fleet,
    ):
        fleet()
        for n in range(5):
            self._convert(pp, n)
        first = self._convert(pp, 5)
        assert first.startswith(b"HTTP/1.1 401"), first[:60]
        assert b"retry-after" not in first.lower()
        for n in (6, 7):
            got = self._convert(pp, n)
            assert got.startswith(b"HTTP/1.1 429"), (n, got[:60])
            lowered = got.lower()
            assert b"anthropic-ratelimit" not in lowered
            assert b"representative-claim" not in lowered
            assert re.findall(rb"(?im)^retry-after: (\d+)\r$", got) == [b"30"]
        assert pp._test_holds == [120.0] * 6

    def test_one_held_401_goes_through_per_spell_while_the_live_slot_is_not_held(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        clock = _SleepingMachine(monkeypatch)
        for n in range(5):
            self._convert(pp, n)
        assert pp._CONVERSION_RELAY_SPELL_S == 300.0
        assert self._convert(pp, 5).startswith(b"HTTP/1.1 401")
        assert pp._test_holds[-1] == 120.0
        clock.sleep_for(299)
        assert self._convert(pp, 6).startswith(b"HTTP/1.1 429")
        clock.sleep_for(2)
        got = self._convert(pp, 7)
        assert got.startswith(b"HTTP/1.1 401"), got[:60]
        assert b"retry-after" not in got.lower()
        assert pp._test_holds[-1] == 120.0
        clock.sleep_for(10)
        assert self._convert(pp, 0).startswith(b"HTTP/1.1 429")

    def test_a_session_keeps_relaying_while_the_live_slot_is_held(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet()
        for n in range(5):
            self._convert(pp, n)
        real = pp._note_conversion

        def note_then_hold_the_live_slot(session):
            count = real(session)
            f.hold_now(f.live_slot())
            return count

        monkeypatch.setattr(pp, "_note_conversion", note_then_hold_the_live_slot)
        for n in (5, 6):
            got = self._convert(pp, n)
            assert got.startswith(b"HTTP/1.1 429"), (n, got[:60])
        assert pp._walled_relay_spell_by_session == {}
        assert not [k for k in pp._walled_switch_seen_by_session
                    if k[0] in (self.RESETS[5], self.RESETS[6])]

    def test_a_request_without_a_session_id_is_never_counted_or_held(
        self, pp, fleet,
    ):
        fleet()
        for n in range(8):
            assert self._convert(pp, n, session="").startswith(
                b"HTTP/1.1 401")
        assert pp._test_holds == []
        assert pp._walled_conversions_by_session == {}

    def test_conversions_older_than_1800_seconds_leave_the_count(self, pp, fleet):
        fleet()
        now = time.time()
        pp._walled_conversions_by_session["s1"] = [now - 1801.0] * 4 + [now - 5.0]
        got = self._convert(pp, 0)
        assert got.startswith(b"HTTP/1.1 401")
        assert pp._test_holds == []
        assert len(pp._walled_conversions_by_session["s1"]) == 2

    def test_a_session_with_nothing_left_in_the_window_is_dropped(self, pp, fleet):
        fleet()
        pp._walled_conversions_by_session["gone"] = [time.time() - 1900.0]
        self._convert(pp, 0, session="fresh")
        assert set(pp._walled_conversions_by_session) == {"fresh"}

    def test_each_session_counts_alone(self, pp, fleet):
        fleet()
        for n in range(3):
            self._convert(pp, n, session=f"s{n}")
        assert pp._test_holds == []

    def test_the_count_is_taken_under_the_wall_lock(self, pp, fleet, monkeypatch):
        fleet()
        seen: list[bool] = []
        real = pp._note_conversion

        def spy(session):
            seen.append(pp._walled_switch_lock.locked())
            return real(session)

        monkeypatch.setattr(pp, "_note_conversion", spy)
        self._convert(pp, 0)
        assert seen == [True]

    def test_the_hold_runs_on_the_relay_path_holding_no_lock(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        locked: list[bool] = []
        monkeypatch.setattr(
            pp, "_hold_reply",
            lambda seconds: locked.append(pp._walled_switch_lock.locked()))
        for n in range(3):
            self._convert(pp, n)
        assert locked == [False]

    def test_a_draining_process_ends_the_hold_at_once(self, pp, monkeypatch):
        def never_sleep(seconds):
            raise AssertionError(f"slept {seconds}s while draining")

        monkeypatch.setattr(pp, "this_process_is_draining", lambda: True)
        monkeypatch.setattr(time, "sleep", never_sleep)
        pp._test_real_hold_reply(120.0)

    def test_a_process_that_starts_draining_mid_hold_stops_sleeping(
        self, pp, monkeypatch,
    ):
        clock = _VirtualClock()
        monkeypatch.setattr(time, "monotonic", clock.monotonic)
        monkeypatch.setattr(time, "sleep", clock.sleep)
        monkeypatch.setattr(
            pp, "this_process_is_draining", lambda: clock.m >= 1010.0)
        pp._test_real_hold_reply(120.0)
        assert 1010.0 <= clock.m < 1011.0, clock.m

    def test_an_undisturbed_hold_lasts_its_whole_length(self, pp, monkeypatch):
        clock = _VirtualClock()
        monkeypatch.setattr(time, "monotonic", clock.monotonic)
        monkeypatch.setattr(time, "sleep", clock.sleep)
        monkeypatch.setattr(pp, "this_process_is_draining", lambda: False)
        pp._test_real_hold_reply(120.0)
        assert clock.m == pytest.approx(1120.0)

    def test_a_draining_process_relays_the_reply_without_holding(
        self, pp, fleet, monkeypatch,
    ):
        fleet()

        def never_sleep(seconds):
            raise AssertionError(f"slept {seconds}s while draining")

        monkeypatch.setattr(pp, "_hold_reply", pp._test_real_hold_reply)
        monkeypatch.setattr(pp, "this_process_is_draining", lambda: True)
        monkeypatch.setattr(time, "sleep", never_sleep)
        for n in range(4):
            got = self._convert(pp, n)
            assert got.startswith(b"HTTP/1.1 401"), (n, got[:60])


class TestARepeatInsideTheTtlGetsTheWaitingRelayAndASiblingItsOwn401:
    def test_a_repeat_of_the_same_bearer_inside_the_ttl_gets_the_waiting_relay(
        self, pp, fleet,
    ):
        fleet()
        statuses = []
        for _ in range(11):
            got = _relay(pp, reset=RESET_1, auth="Bearer sk-foreign",
                         session="frozen")
            statuses.append(int(got[9:12]))
            if statuses[-1] == 429:
                assert re.findall(
                    rb"(?im)^retry-after: (\d+)\r$", got) == [b"30"]
                assert b"anthropic-ratelimit" not in got.lower()
        assert statuses == [401] + [429] * 10, statuses
        assert pp._test_holds == [120.0] * 10

    def test_the_session_entry_lasts_the_shared_ttl_and_no_longer(
        self, pp, fleet,
    ):
        fleet()
        _relay(pp, reset=RESET_1, auth="Bearer sk-foreign", session="frozen")
        (expiry,) = pp._walled_switch_seen_by_session.values()
        assert 20.0 < expiry - time.time() <= 30.0
        assert pp._WALLED_SWITCH_RAISE_TTL == 30.0
        assert not hasattr(pp, "_SESSION_CONVERTED_TTL_S")

    def test_a_repeat_after_the_ttl_gets_a_401_again(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        clock = _SleepingMachine(monkeypatch)
        first = _relay(pp, reset=RESET_1, auth="Bearer sk-foreign",
                       session="sibling")
        assert first.startswith(b"HTTP/1.1 401"), first[:60]
        inside = _relay(pp, reset=RESET_1, auth="Bearer sk-foreign",
                        session="sibling")
        assert inside.startswith(b"HTTP/1.1 429"), inside[:60]
        clock.sleep_for(31)
        after = _relay(pp, reset=RESET_1, auth="Bearer sk-foreign",
                       session="sibling")
        assert after.startswith(b"HTTP/1.1 401"), after[:60]

    def test_another_session_on_the_same_wall_still_gets_its_own_401(
        self, pp, fleet,
    ):
        fleet()
        for session in ("a", "b"):
            got = _relay(pp, reset=RESET_1, auth="Bearer sk-foreign",
                         session=session)
            assert got.startswith(b"HTTP/1.1 401"), (session, got[:60])


class _VirtualClock:
    def __init__(self) -> None:
        self.t = time.time()
        self.m = 1000.0

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.m

    def sleep(self, seconds: float) -> None:
        step = max(seconds, 0.0)
        self.t += step
        self.m += step


def _client_backoff(attempt: int, rng) -> float:
    return min(0.5 * 2 ** (attempt - 1), 32.0) * (1 + rng.random() * 0.25)


class TestAFleetWideThrottleNeverEndsInATerminal401:
    @pytest.mark.parametrize("accounts", [5, 8])
    @pytest.mark.parametrize("throttle_s", [120, 300, 600])
    def test_claude_codes_eleven_attempts_never_end_on_a_401(
        self, pp, fleet, monkeypatch, accounts, throttle_s,
    ):
        trace = self._attempts(pp, fleet, monkeypatch, accounts, throttle_s)
        assert trace[-1][2] != 401, trace

    @pytest.mark.parametrize("accounts", [5, 8])
    def test_an_hour_long_throttle_ends_every_attempt_in_a_real_answer(
        self, pp, fleet, monkeypatch, accounts,
    ):
        trace = self._attempts(pp, fleet, monkeypatch, accounts, 3600)
        assert [t[0] for t in trace] == list(range(1, 12)), trace
        assert 200 not in {t[2] for t in trace}, trace
        assert {t[2] for t in trace} == {401, 429}, trace
        assert [t[2] for t in trace[5:]].count(401) <= 3, trace

    @staticmethod
    def _attempts(pp, fleet, monkeypatch, accounts, throttle_s):
        import random

        f = fleet(count=accounts, usage={
            str(i): _usage(10 + i) for i in range(1, accounts + 1)})
        clock = _VirtualClock()
        for name in ("time", "monotonic", "sleep"):
            monkeypatch.setattr(time, name, getattr(clock, name))
        monkeypatch.setattr(pp, "_hold_reply", pp._test_real_hold_reply)
        monkeypatch.setattr(
            pp, "_hold_relay_wait", pp._test_real_hold_relay_wait)
        rng = random.Random(7)
        start = clock.time()
        bearer = "sk-live"
        trace = []
        for attempt in range(1, 12):
            slot = "1" if bearer == "sk-live" else bearer.split("-")[1]
            if clock.time() - start >= throttle_s:
                trace.append((attempt, slot, 200))
                break
            reset = str(1_900_000_000 + int(slot) * 100).encode()
            got = _relay(pp, reset=reset, retry_after=b"",
                         auth=f"Bearer {bearer}", session="cc")
            status = int(got[9:12])
            advised = re.findall(rb"(?im)^retry-after: (\d+)\r$", got)
            trace.append((attempt, slot, status))
            assert b"representative-claim" not in got
            if attempt == 11:
                break
            if status == 401:
                bearer = pp._active_oauth_token() or bearer
            clock.sleep(max(int(advised[0]) if advised else 0,
                            _client_backoff(attempt, rng)))
        return trace


def _race(pp, fleet, monkeypatch, clients, bearer, frozen=False):
    """CC's retry loop for ``clients`` requests of one session id on a shared
    bearer, over the real relay and switcher with a virtual clock. Slot 1 is
    walled for good; every other slot answers 200. A 401 rebuilds a client
    onto the live token unless ``frozen``. Returns one trace per client of
    ``(attempt, seconds since start, status, seconds held)``."""
    import heapq
    import random

    fleet(count=5, usage={str(i): _usage(10 + i) for i in range(1, 6)})
    clock = _VirtualClock()
    for name in ("time", "monotonic", "sleep"):
        monkeypatch.setattr(time, name, getattr(clock, name))
    held = [0.0]

    def hold(seconds):
        held[0] += max(seconds, 0.0)
        return pp._fleet_held_until

    monkeypatch.setattr(pp, "_hold_reply", hold)
    monkeypatch.setattr(pp, "_hold_relay_wait", hold)
    rng = random.Random(3)
    start = clock.time()
    bearers = [bearer] * clients
    traces: list[list[tuple]] = [[] for _ in range(clients)]
    queue = [(start, i) for i in range(clients)]
    heapq.heapify(queue)
    while queue:
        at, i = heapq.heappop(queue)
        clock.m += at - clock.t
        clock.t = at
        attempt = len(traces[i]) + 1
        slot = "1" if bearers[i] == "sk-live" else bearers[i].split("-")[1]
        if slot != "1":
            traces[i].append((attempt, round(at - start, 1), 200, 0.0))
            continue
        held[0] = 0.0
        got = _relay(pp, reset=b"1900000100", retry_after=b"",
                     auth=f"Bearer {bearers[i]}", session="cc")
        assert b"representative-claim" not in got
        status = int(got[9:12])
        advised = re.findall(rb"(?im)^retry-after: (\d+)\r$", got)
        traces[i].append((attempt, round(at - start, 1), status, held[0]))
        if attempt == 11:
            continue
        if status == 401 and not frozen:
            bearers[i] = pp._active_oauth_token() or bearers[i]
        wait = held[0] + max(int(advised[0]) if advised else 0,
                             _client_backoff(attempt, rng))
        heapq.heappush(queue, (at + wait, i))
    return traces


class TestParallelSiblingsOfOneSessionAllReachAHealthySlot:
    @pytest.mark.parametrize("clients", [3, 6])
    def test_every_sibling_ends_on_a_200_with_one_slot_walled_for_good(
        self, pp, fleet, monkeypatch, clients,
    ):
        traces = _race(pp, fleet, monkeypatch, clients, "sk-live")
        for i, trace in enumerate(traces):
            assert trace[-1][2] == 200, (i, trace)
            assert len(trace) <= 11, (i, trace)
        ended = max(trace[-1][1] for trace in traces)
        assert ended < (400 if clients == 3 else 900), (ended, traces)

    def test_a_truly_frozen_bearer_is_bounded_by_the_counter_and_the_clients_budget(
        self, pp, fleet, monkeypatch,
    ):
        (trace,) = _race(
            pp, fleet, monkeypatch, 1, "sk-live", frozen=True)
        statuses = [t[2] for t in trace]
        assert len(trace) == 11, trace
        assert set(statuses) <= {401, 429}, trace
        assert statuses[-1] != 200, trace
        late = [t[1] for t in trace if t[2] == 401][5:]
        assert late, trace
        assert all(b - a >= 300 for a, b in zip(late, late[1:])), trace


class _SleepingMachine:
    """Only ``time.time`` moves: what a Mac that slept does to the clocks."""

    def __init__(self, monkeypatch) -> None:
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + self.offset)

    def sleep_for(self, seconds: float) -> None:
        self.offset += seconds


class TestTheSessionMemosFollowTheWallClockAndStayBounded:
    RESETS = TestConversionsDoNotEatTheClientsRetryBudget.RESETS

    def _stale(self, pp, n, session="s1", bearer="sk-old"):
        return _relay(
            pp, reset=self.RESETS[n], auth=f"Bearer {bearer}", session=session)

    def test_a_conversion_turned_into_the_waiting_relay_leaves_no_memo(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        clock = _SleepingMachine(monkeypatch)
        for n in range(5):
            assert self._stale(pp, n).startswith(b"HTTP/1.1 401"), n
        clock.sleep_for(100)
        spell = self._stale(pp, 5)
        assert spell.startswith(b"HTTP/1.1 401"), spell[:60]
        assert [k for k in pp._walled_switch_seen_by_session
                if k[0] == self.RESETS[5]]
        got = self._stale(pp, 6)
        assert got.startswith(b"HTTP/1.1 429"), got[:60]
        assert not [k for k in pp._walled_switch_seen_by_session
                    if k[0] == self.RESETS[6]]
        for _ in range(6):
            assert self._stale(pp, 6).startswith(b"HTTP/1.1 429")
        clock.sleep_for(1750)
        got = self._stale(pp, 6)
        assert got.startswith(b"HTTP/1.1 401"), got[:60]

    def test_a_429_on_the_live_token_clears_that_sessions_memos_only(
        self, pp, fleet,
    ):
        fleet()
        self._stale(pp, 0, session="a")
        self._stale(pp, 1, session="b")
        assert {k[2] for k in pp._walled_switch_seen_by_session} == {"a", "b"}
        got = _relay(pp, reset=RESET_3, auth="Bearer sk-live", session="a")
        assert got.startswith(b"HTTP/1.1 401"), got[:60]
        assert {k[2] for k in pp._walled_switch_seen_by_session} == {"b"}

    def test_a_session_that_rebuilt_gets_a_new_401_when_the_old_wall_returns(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet(count=2, usage={"1": _usage(10), "2": _usage(20)})
        clock = _SleepingMachine(monkeypatch)
        first = self._stale(pp, 0, session="cc", bearer="sk-2")
        assert first.startswith(b"HTTP/1.1 401"), first[:60]
        rebuilt = _relay(pp, reset=RESET_1, auth="Bearer sk-live", session="cc")
        assert rebuilt.startswith(b"HTTP/1.1 401"), rebuilt[:60]
        assert f.active() == 2
        clock.sleep_for(200)
        ClaudeAccountSwitcher().switch_to("1", json_output=True)
        again = self._stale(pp, 0, session="cc", bearer="sk-2")
        assert again.startswith(b"HTTP/1.1 401"), again[:60]

    def test_a_changed_bearer_on_the_same_wall_is_not_frozen(self, pp, fleet):
        fleet()
        for bearer in ("sk-old-a", "sk-old-b"):
            got = _relay(pp, reset=RESET_1, auth=f"Bearer {bearer}", session="s1")
            assert got.startswith(b"HTTP/1.1 401"), (bearer, got[:60])
        again = _relay(pp, reset=RESET_1, auth="Bearer sk-old-b", session="s1")
        assert again.startswith(b"HTTP/1.1 429"), again[:60]
        (key,) = pp._walled_switch_seen_by_session
        assert key[3] == pp._bearer_digest("sk-old-b")

    def test_a_second_throttle_on_the_same_wall_after_a_rebuild_is_not_terminal(
        self, pp, fleet,
    ):
        fleet()
        first = _relay(pp, reset=RESET_1, auth="Bearer sk-old-a", session="s1")
        assert first.startswith(b"HTTP/1.1 401"), first[:60]
        other = _relay(pp, reset=RESET_1, auth="Bearer sk-old-b", session="s1")
        assert other.startswith(b"HTTP/1.1 401"), other[:60]
        assert {k[3] for k in pp._walled_switch_seen_by_session} == {
            pp._bearer_digest("sk-old-b")}
        again = _relay(pp, reset=RESET_1, auth="Bearer sk-old-a", session="s1")
        assert again.startswith(b"HTTP/1.1 401"), again[:60]
        assert {k[3] for k in pp._walled_switch_seen_by_session} == {
            pp._bearer_digest("sk-old-a")}

    def test_the_memos_of_another_session_survive_a_changed_bearer(
        self, pp, fleet,
    ):
        fleet()
        _relay(pp, reset=RESET_1, auth="Bearer sk-old-a", session="s1")
        _relay(pp, reset=RESET_1, auth="Bearer sk-old-b", session="s2")
        assert {(k[2], k[3]) for k in pp._walled_switch_seen_by_session} == {
            ("s1", pp._bearer_digest("sk-old-a")),
            ("s2", pp._bearer_digest("sk-old-b"))}

    def test_a_none_slot_read_never_gives_uncounted_401s(self, pp, fleet, monkeypatch):
        fleet()
        monkeypatch.setattr(pp, "_live_account_slot", lambda: None)
        statuses = []
        for _ in range(11):
            statuses.append(int(_relay(
                pp, reset=RESET_1, auth="Bearer sk-live", session="s1")[9:12]))
        assert statuses == [401] * 6 + [429] * 5, statuses
        assert len(pp._walled_conversions_by_session["s1"]) == 5

    def test_the_bearer_itself_is_never_stored_or_logged(self, pp, fleet):
        fleet()
        for bearer in ("sk-old-a", "sk-old-b"):
            _relay(pp, reset=RESET_1, auth=f"Bearer {bearer}", session="s1")
        for key in pp._walled_switch_seen_by_session:
            assert "sk-old" not in repr(key)
            assert len(key[3]) <= 16
        assert not [line for line in pp._test_lines if "sk-old" in line]

    def test_eight_parallel_requests_on_one_landed_wall_all_get_an_instant_401(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet(count=5, usage={
            str(i): _usage(10 + i) for i in range(1, 6)})
        reads = threading.Semaphore(0)
        real_token = pp._active_oauth_token

        def token_then_signal():
            token = real_token()
            reads.release()
            return token

        monkeypatch.setattr(pp, "_active_oauth_token", token_then_signal)
        replies: list = []
        guard = threading.Lock()

        def one():
            verdict = _switch_off(pp, reset=RESET_1, auth="Bearer sk-live",
                                  session="cc")
            with guard:
                replies.append(verdict)

        pp._walled_switch_lock.acquire()
        workers = [threading.Thread(target=one) for _ in range(8)]
        try:
            for worker in workers:
                worker.start()
            for _ in range(8):
                assert reads.acquire(timeout=10), "a worker never read its pair"
        finally:
            pp._walled_switch_lock.release()
        for worker in workers:
            worker.join(30)
            assert not worker.is_alive()
        assert replies == [True] * 8, replies
        assert len(f.switch_calls) == 1
        assert pp._test_holds == []

    def test_the_memo_follows_the_wall_clock_not_the_monotonic_one(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        clock = _SleepingMachine(monkeypatch)
        assert self._stale(pp, 0).startswith(b"HTTP/1.1 401")
        assert self._stale(pp, 0).startswith(b"HTTP/1.1 429")
        clock.sleep_for(31)
        assert self._stale(pp, 0).startswith(b"HTTP/1.1 401")

    def test_the_conversion_window_is_1800_wall_clock_seconds(
        self, pp, fleet, monkeypatch,
    ):
        fleet()
        clock = _SleepingMachine(monkeypatch)
        for n in range(5):
            assert self._stale(pp, n).startswith(b"HTTP/1.1 401"), n
        clock.sleep_for(1200)
        assert self._stale(pp, 5).startswith(b"HTTP/1.1 401")
        assert self._stale(pp, 6).startswith(b"HTTP/1.1 429")
        clock.sleep_for(700)
        assert self._stale(pp, 6).startswith(b"HTTP/1.1 401")
        assert pp._test_holds == [120.0] * 5

    def test_both_session_dicts_are_capped(self, pp, fleet, monkeypatch):
        fleet()
        monkeypatch.setattr(pp, "_SESSION_MEMO_CAP", 3)
        for n in range(5):
            self._stale(pp, 0, session=f"s{n}")
        assert len(pp._walled_switch_seen_by_session) == 3
        assert len(pp._walled_conversions_by_session) == 3
        assert {k[2] for k in pp._walled_switch_seen_by_session} == {
            "s2", "s3", "s4"}
        assert set(pp._walled_conversions_by_session) == {"s2", "s3", "s4"}

    def test_a_frozen_repeat_on_a_held_fleet_waits_the_reply_hold_not_the_fleet(
        self, pp, fleet, monkeypatch,
    ):
        f = fleet()
        assert self._stale(pp, 0, session="frozen").startswith(b"HTTP/1.1 401")
        for slot in ("1", "2", "3"):
            f.hold_now(slot)
        exhausted = _relay(
            pp, reset=RESET_2, auth="Bearer sk-live", session="other")
        assert exhausted.startswith(b"HTTP/1.1 429"), exhausted[:60]
        assert pp._fleet_held_until > time.time()
        assert pp._test_waits, "the fleet-wide wait never ran"
        pp._test_waits.clear()
        pp._test_holds.clear()
        got = self._stale(pp, 0, session="frozen")
        assert got.startswith(b"HTTP/1.1 429"), got[:60]
        assert re.findall(rb"(?im)^retry-after: (\d+)\r$", got) == [b"30"]
        assert pp._test_waits == []
        assert pp._test_holds == [120.0]
