"""Accounts whose usage carries no ``five_hour`` window (verified Ultra: the
API omits it) have no 5h limit: they are eligible and ranked on the weekly and
scoped windows only, never read as unknown or blocked."""

from __future__ import annotations

import pytest

from claude_swap import balance, oauth, poll_policy
from claude_swap.autoswitch import TickOutcome
from claude_swap.balance import BalanceParams
from claude_swap.tui import data as tui_data

from tests.test_autoswitch import EngineHarness, _iso_at, _usage

NOW = 1_000_000.0
DAY = 86400.0


def _weekly_only(pct7: float, resets_in: float | None = None, scoped=None) -> dict:
    seven: dict = {"pct": pct7}
    if resets_in is not None:
        seven["resets_at"] = _iso_at(NOW + resets_in)
    usage: dict = {"seven_day": seven}
    if scoped is not None:
        usage["scoped"] = scoped
    return usage


class TestPolicyPrimitives:
    def test_headroom_comes_from_the_weekly_window(self):
        assert oauth.account_headroom(_weekly_only(30.0)) == pytest.approx(70.0)

    def test_scoped_window_binds_only_when_the_model_is_configured(self):
        usage = _weekly_only(
            30.0, scoped=[{"name": "Fable", "pct": 95.0, "resets_at": None}]
        )
        assert oauth.account_headroom(usage) == pytest.approx(70.0)
        assert oauth.account_headroom(usage, ("Fable",)) == pytest.approx(5.0)

    def test_recovery_ts_is_the_weekly_reset(self):
        usage = _weekly_only(100.0, resets_in=2 * DAY)
        assert poll_policy.binding_recovery_ts(usage, (), NOW) == pytest.approx(
            NOW + 2 * DAY, abs=1
        )

    def test_balance_scores_it_eligible(self):
        score = balance.score_account(
            "1",
            _weekly_only(20.0, resets_in=3 * DAY),
            now=NOW,
            models=(),
            busy_sessions=2,
            params=BalanceParams(),
        )
        assert score.eligible
        assert score.reason == balance.REASON_OK
        assert score.headroom == pytest.approx(80.0)

    def test_balance_gates_it_on_the_weekly_threshold(self):
        score = balance.score_account(
            "1",
            _weekly_only(95.0, resets_in=3 * DAY),
            now=NOW,
            models=(),
            busy_sessions=0,
            params=BalanceParams(),
        )
        assert not score.eligible
        assert score.reason == balance.REASON_WEEKLY_THRESHOLD


class TestEngine:
    def _fleet(self, temp_home, n: int = 3, **settings) -> EngineHarness:
        h = EngineHarness(temp_home, **settings)
        for num in range(1, n + 1):
            h.seed(num, f"{num}@example.com")
        h.make_live("1@example.com", 1)
        return h

    def test_best_switches_onto_a_no_five_hour_peer(self, temp_home):
        h = self._fleet(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _usage(95.0),
            "2": _weekly_only(20.0, resets_in=3 * DAY),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_best_ranks_no_five_hour_peer_on_weekly_headroom(self, temp_home):
        h = self._fleet(temp_home, 3)
        outcome = h.tick_with_usage({
            "1": _usage(95.0),
            "2": _weekly_only(60.0, resets_in=3 * DAY),
            "3": _weekly_only(10.0, resets_in=3 * DAY),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_consume_first_picks_the_soonest_weekly_reset(self, temp_home):
        h = self._fleet(temp_home, 3, strategy="consume-first")
        outcome = h.tick_with_usage({
            "1": _usage(95.0),
            "2": _weekly_only(30.0, resets_in=1 * DAY),
            "3": {
                "five_hour": {"pct": 10.0},
                "seven_day": {"pct": 30.0, "resets_at": _iso_at(NOW + 5 * DAY)},
            },
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_balance_switches_onto_a_no_five_hour_peer(self, temp_home):
        h = self._fleet(temp_home, 2, strategy="balance")
        outcome = h.tick_with_usage({
            "1": _usage(95.0),
            "2": _weekly_only(5.0, resets_in=3 * DAY),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_without_five_hour_below_weekly_line_stays(self, temp_home):
        h = self._fleet(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _weekly_only(20.0, resets_in=3 * DAY),
            "2": _weekly_only(10.0, resets_in=3 * DAY),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_active_without_five_hour_over_weekly_line_leaves(self, temp_home):
        h = self._fleet(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _weekly_only(95.0, resets_in=3 * DAY),
            "2": _usage(10.0),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


def _with_five_hour(pct5: float | None, pct7: float = 40.0) -> dict:
    usage: dict = {
        "seven_day": {"pct": pct7, "resets_at": _iso_at(NOW + 3 * DAY)},
    }
    if pct5 is not None:
        usage = {"five_hour": {"pct": pct5}, **usage}
    return usage


class TestBalanceWithoutFiveHour:
    def _score(self, usage, busy):
        return balance.score_account(
            "1", usage, now=NOW, models=(), busy_sessions=busy, params=BalanceParams()
        )

    def test_busy_sessions_add_no_projection_or_penalty(self):
        idle = self._score(_with_five_hour(None), 0)
        busy = self._score(_with_five_hour(None), 9)
        assert busy.projected_5h == 0.0
        assert busy.score == pytest.approx(idle.score)
        assert busy.eligible
        assert busy.reason == balance.REASON_OK

    def test_present_zero_five_hour_still_projects_and_gates(self):
        busy = self._score(_with_five_hour(0.0), 6)
        assert busy.projected_5h == pytest.approx(90.0)
        assert not busy.eligible
        assert busy.reason == balance.REASON_FIVE_HOUR_CEILING

    def test_engine_balance_ranks_without_five_hour_penalty(self, temp_home):
        h = EngineHarness(temp_home, strategy="balance")
        for num in (1, 2):
            h.seed(num, f"{num}@example.com")
        h.make_live("1@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage(95.0),
            "2": _weekly_only(5.0, resets_in=3 * DAY),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestBuildUsageResult:
    @pytest.mark.parametrize(
        "five_hour",
        [None, {}, {"utilization": None, "resets_at": None}],
    )
    def test_missing_or_null_five_hour_yields_no_window(self, five_hour):
        data = {
            "five_hour": five_hour,
            "seven_day": {"utilization": 10.0, "resets_at": None},
        }
        result = oauth.build_usage_result(data)
        assert "five_hour" not in result
        assert result["seven_day"]["pct"] == 10.0

    def test_absent_five_hour_key_yields_no_window(self):
        data = {"seven_day": {"utilization": 10.0}}
        assert "five_hour" not in data
        assert "five_hour" not in oauth.build_usage_result(data)

    @pytest.mark.parametrize("utilization", [True, False])
    def test_bool_utilization_is_not_a_percentage(self, utilization):
        data = {
            "five_hour": {"utilization": utilization, "resets_at": None},
            "seven_day": {"utilization": 10.0},
        }
        assert "five_hour" not in oauth.build_usage_result(data)

    def test_stored_null_pct_window_renders_nothing(self):
        from claude_swap.switcher import _format_usage_lines
        from claude_swap.tui.widgets import mini_account_text, usage_rows

        usage = {"five_hour": {"pct": None}, "seven_day": {"pct": 40.0}}
        assert [row[0] for row in usage_rows(usage, NOW)] == ["7d"]
        assert [line.split(":")[0] for line in _format_usage_lines(usage)] == ["7d"]
        from tests.test_tui import make_account

        from claude_swap.usage_store import UsageEntry

        acc = make_account(
            "1", entry=UsageEntry(last_good=usage, fetched_at=NOW, age_s=1.0)
        )
        plain = mini_account_text(acc, NOW).plain
        assert "5h" not in plain
        assert "7d 40%" in plain

    def test_present_zero_without_reset_keeps_the_window(self):
        data = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 10.0, "resets_at": None},
        }
        result = oauth.build_usage_result(data)
        assert result["five_hour"] == {"pct": 0.0}


class TestListSurfaces:
    def test_text_lines_omit_five_hour(self):
        from claude_swap.switcher import _format_usage_lines

        lines = _format_usage_lines(_with_five_hour(None))
        assert len(lines) == 1
        assert lines[0].startswith("7d:")

    def test_text_lines_keep_present_zero_five_hour(self):
        from claude_swap.switcher import _format_usage_lines

        lines = _format_usage_lines(_with_five_hour(0.0))
        assert [line.split(":")[0] for line in lines] == ["5h", "7d"]
        assert "  0%" in lines[0]

    def test_json_omits_five_hour(self):
        from claude_swap.json_output import usage_to_json

        out = usage_to_json(_with_five_hour(None))
        assert "fiveHour" not in out
        assert "sevenDay" in out

    def test_json_keeps_present_zero_five_hour(self):
        from claude_swap.json_output import usage_to_json

        out = usage_to_json(_with_five_hour(0.0))
        assert out["fiveHour"]["pct"] == 0.0


class TestTuiSurfaces:
    def _account(self, pct5):
        from tests.test_tui import make_account

        from claude_swap.usage_store import UsageEntry

        return make_account(
            "1",
            active=True,
            entry=UsageEntry(
                last_good=_with_five_hour(pct5), fetched_at=NOW, age_s=1.0
            ),
        )

    def test_usage_rows_omit_five_hour(self):
        from claude_swap.tui.widgets import usage_rows

        rows = usage_rows(_with_five_hour(None), NOW)
        assert [row[0] for row in rows] == ["7d"]

    def test_usage_rows_keep_present_zero_five_hour(self):
        from claude_swap.tui.widgets import usage_rows

        rows = usage_rows(_with_five_hour(0.0), NOW)
        assert [row[0] for row in rows] == ["5h", "7d"]

    def test_card_omits_five_hour_row(self):
        from claude_swap.tui.widgets import account_card_text

        plain = account_card_text(self._account(None), 100, now=NOW).plain
        assert "5h" not in plain
        assert "7d" in plain

    def test_card_keeps_present_zero_five_hour_row(self):
        from claude_swap.tui.widgets import account_card_text

        plain = account_card_text(self._account(0.0), 100, now=NOW).plain
        assert "5h" in plain

    def test_minimized_row_omits_five_hour(self):
        from claude_swap.tui.widgets import mini_account_text

        plain = mini_account_text(self._account(None), NOW).plain
        assert "5h" not in plain
        assert "7d 40%" in plain

    def test_minimized_row_keeps_present_zero_five_hour(self):
        from claude_swap.tui.widgets import mini_account_text

        plain = mini_account_text(self._account(0.0), NOW).plain
        assert "5h 0%" in plain

    def test_next_best_binds_on_the_weekly_window(self):
        from claude_swap.lines import binding_window

        binding = binding_window(
            oauth.relevant_windows(_with_five_hour(None, 40.0), ()), 90.0, None
        )
        assert binding is not None
        assert binding.label == "7d"

    def test_next_best_keeps_five_hour_when_present(self):
        from claude_swap.lines import binding_window

        binding = binding_window(
            oauth.relevant_windows(_with_five_hour(70.0, 40.0), ()), 90.0, None
        )
        assert binding.label == "5h"


class TestMenubarSurfaces:
    def _settings(self, mode="both"):
        from claude_swap.menubar import MenuBarSettings

        return MenuBarSettings(title_pct=mode)

    def test_summary_omits_five_hour(self):
        from claude_swap.menubar import usage_summary

        text = usage_summary(_with_five_hour(None), now=NOW)
        assert "5h" not in text
        assert text.startswith("7d 40%")

    def test_summary_keeps_present_zero_five_hour(self):
        from claude_swap.menubar import usage_summary

        assert "5h 0%" in usage_summary(_with_five_hour(0.0), now=NOW)

    def test_title_has_only_the_weekly_figure(self):
        from claude_swap.menubar import ICON, format_title

        title = format_title(
            "a@example.com", _with_five_hour(None), self._settings(), now=NOW
        )
        assert title == f"{ICON} a · 40%"

    def test_title_five_hour_mode_without_the_window_shows_no_figure(self):
        from claude_swap.menubar import ICON, format_title

        title = format_title(
            "a@example.com", _with_five_hour(None), self._settings("5h"), now=NOW
        )
        assert title == f"{ICON} a"

    def test_title_keeps_present_zero_five_hour(self):
        from claude_swap.menubar import ICON, format_title

        title = format_title(
            "a@example.com", _with_five_hour(0.0), self._settings(), now=NOW
        )
        assert title == f"{ICON} a · 0% · 40%"

    def test_usage_log_and_dedupe_key(self):
        from claude_swap.menubar import _usage_log_key, format_usage_log, tightest_pct

        usage = _with_five_hour(None)
        line = format_usage_log("a@example.com", usage)
        assert "5h" not in line
        assert "7d 40%" in line
        assert _usage_log_key(usage) == (None, 40.0)
        assert tightest_pct(usage) == 40.0
        assert "5h 0%" in format_usage_log("a@example.com", _with_five_hour(0.0))


class TestEventSurfaces:
    def test_window_pcts_omit_five_hour(self):
        from claude_swap.autoswitch import _window_pcts

        assert list(_window_pcts(_with_five_hour(None))) == ["7d"]
        assert list(_window_pcts(_with_five_hour(0.0))) == ["5h", "7d"]

    def test_poll_event_describes_only_present_windows(self):
        from claude_swap.autoswitch import PollEvent

        poll = PollEvent(
            active={"number": 1, "email": "a@example.com"},
            headroom={"1": 60.0, "2": 60.0},
            threshold=90.0,
            windows={"1": {"7d": 40.0}, "2": {"5h": 0.0, "7d": 40.0}},
        )
        text = poll.human()
        assert "7d 40% used" in text
        assert "#2: 5h 0% · 7d 40%" in text
        assert "unknown" not in text and "?" not in text


class TestWatchOrdering:
    def test_no_five_hour_account_orders_on_its_weekly_reset(self):
        from tests.test_tui import make_account

        from claude_swap.usage_store import UsageEntry

        late = make_account(
            "1", entry=UsageEntry(last_good=_weekly_only(10.0, resets_in=5 * DAY))
        )
        soon = make_account(
            "2", entry=UsageEntry(last_good=_weekly_only(10.0, resets_in=1 * DAY))
        )
        ordered = tui_data.order_accounts([late, soon], "reset", NOW)
        assert [a.number for a in ordered] == ["2", "1"]
