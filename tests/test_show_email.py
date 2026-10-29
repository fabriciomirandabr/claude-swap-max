"""ui.showEmail: aliased accounts render by alias only when it is off."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import menubar, oauth
from claude_swap.autoswitch import QuarantineEvent, SwitchEvent
from claude_swap.exceptions import ConfigError
from claude_swap.identity import (
    account_display,
    account_parts,
    masked_email,
    show_email_enabled,
)
from claude_swap.settings import (
    UiSettings,
    load_ui_settings,
    set_setting,
    settings_path,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.widgets import account_card_text, mini_account_text
from tests.test_tui import make_account

EMAIL = "work@acme.dev"
ORG = "Acme Organization"


class TestAccountDisplay:
    def test_alias_email_org_shown(self):
        out = account_display("dev", EMAIL, ORG, show_email=True)
        assert out == f"dev ({EMAIL}) [{ORG}]"

    def test_alias_only_when_hidden(self):
        out = account_display("dev", EMAIL, ORG, show_email=False)
        assert out == "dev"

    def test_no_alias_keeps_email_and_org_when_hidden(self):
        out = account_display(None, EMAIL, "personal", show_email=False)
        assert out == f"{EMAIL} [personal]"

    def test_no_alias_shown(self):
        assert account_display("", EMAIL, None, show_email=True) == EMAIL

    def test_styles_wrap_alias_and_org(self):
        out = account_display(
            "dev", EMAIL, ORG, show_email=True,
            alias_style=lambda s: f"<{s}>", org_style=lambda s: f"{{{s}}}",
        )
        assert out == f"<dev> ({EMAIL}) {{[{ORG}]}}"

    def test_parts_and_masked_email(self):
        assert account_parts("dev", EMAIL, ORG, show_email=False) == ("dev", None, None)
        assert account_parts(None, EMAIL, ORG, show_email=False) == (None, EMAIL, ORG)
        assert masked_email("dev", EMAIL, show_email=False) == "dev"
        assert masked_email("dev", EMAIL, show_email=True) == EMAIL
        assert masked_email(None, EMAIL, show_email=False) == EMAIL


class TestSetting:
    def test_default_true(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto", show_email=True)
        assert show_email_enabled(tmp_path) is True

    def test_set_boolean(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.showEmail", "false") is False
        assert json.loads(settings_path(tmp_path).read_text())["ui"] == {
            "showEmail": False
        }
        assert show_email_enabled(tmp_path) is False

    def test_rejects_non_boolean(self, tmp_path: Path):
        with pytest.raises(ConfigError):
            set_setting(tmp_path, "ui.showEmail", "maybe")

    def test_non_boolean_in_file_keeps_default_and_theme(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"ui": {"theme": "light", "showEmail": "no"}})
        )
        loaded = load_ui_settings(tmp_path)
        assert loaded.show_email is True
        assert loaded.theme == "light"


def _list_output(temp_home: Path, data: dict, capsys, *, show_email: bool) -> str:
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, data)
    if not show_email:
        set_setting(switcher.backup_dir, "ui.showEmail", "false")
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
    with patch.object(switcher, "_read_credentials", return_value=creds), patch.object(
        switcher, "_read_account_credentials", return_value=creds
    ), patch(
        "claude_swap.oauth.try_fetch_usage_for_account",
        return_value=oauth.UsageOutcome(None),
    ):
        switcher.list_accounts()
    return capsys.readouterr().out


class TestSurfaces:
    @pytest.fixture
    def data(self, sample_sequence_data_with_org: dict) -> dict:
        sample_sequence_data_with_org["accounts"]["1"]["alias"] = "dev"
        return sample_sequence_data_with_org

    def test_cli_list_shown(
        self, temp_home: Path, mock_org_claude_config: Path, data: dict, capsys
    ):
        out = _list_output(temp_home, data, capsys, show_email=True)
        assert "1: dev (user@example.com) [Acme Corp] (active)" in out
        assert "2: user@example.com [personal]" in out

    def test_cli_list_hidden(
        self, temp_home: Path, mock_org_claude_config: Path, data: dict, capsys
    ):
        out = _list_output(temp_home, data, capsys, show_email=False)
        assert "1: dev (active)" in out
        assert "Acme Corp" not in out
        assert "2: user@example.com [personal]" in out

    def test_cli_disable_message_hidden(
        self, temp_home: Path, mock_org_claude_config: Path, data: dict, capsys
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)
        set_setting(switcher.backup_dir, "ui.showEmail", "false")
        switcher.set_account_disabled("1", True)
        out = capsys.readouterr().out
        assert "Account-1 (dev)" in out
        assert "user@example.com" not in out

    def test_tui_card_and_mini(self):
        acc = make_account(1, email=EMAIL, alias="dev")
        for render in (
            lambda **kw: account_card_text(acc, 80, **kw).plain,
            lambda **kw: mini_account_text(acc, 0.0, **kw).plain,
        ):
            shown = render(show_email=True)
            assert f"dev ({EMAIL})" in shown
            assert "[personal]" in shown
            hidden = render(show_email=False)
            assert "dev" in hidden
            assert EMAIL not in hidden
            assert "[personal]" not in hidden

    def test_tui_card_without_alias_keeps_email(self):
        acc = make_account(1, email=EMAIL)
        assert EMAIL in account_card_text(acc, 80, show_email=False).plain

    def test_menubar_label(self):
        shown = menubar.format_account_label(2, EMAIL, None, alias="dev")
        assert shown.startswith(f"2  dev  ({EMAIL})")
        hidden = menubar.format_account_label(
            2, EMAIL, None, alias="dev", show_email=False
        )
        assert hidden.startswith("2  dev")
        assert EMAIL not in hidden
        bare = menubar.format_account_label(2, EMAIL, None, show_email=False)
        assert EMAIL in bare

    def test_events_hide_email(self):
        quarantine = QuarantineEvent(number="2", email=EMAIL, reason="revoked")
        assert EMAIL in quarantine.human()
        hidden = quarantine.human(show_email=False)
        assert EMAIL not in hidden
        assert hidden.startswith("Account-2 quarantined")
        switch = SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@b.c"},
            to_ref={"number": 2, "email": EMAIL},
        )
        assert EMAIL in switch.human()
        assert EMAIL not in switch.human(show_email=False)
        assert quarantine.to_json()["email"] == EMAIL


class TestRemainingSurfaces:
    @pytest.fixture
    def data(self, sample_sequence_data_with_org: dict) -> dict:
        sample_sequence_data_with_org["accounts"]["1"]["alias"] = "dev"
        return sample_sequence_data_with_org

    @pytest.fixture
    def switcher(self, temp_home: Path, data: dict) -> ClaudeAccountSwitcher:
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sw._write_json(sw.sequence_file, data)
        return sw

    def test_status_shown_matches_head_format(
        self, switcher, mock_org_claude_config: Path, capsys
    ):
        switcher.status()
        out = capsys.readouterr().out
        assert "Account-1 (user@example.com [Acme Corp])" in out
        assert "dev" not in out

    def test_status_hidden_uses_alias(
        self, switcher, mock_org_claude_config: Path, capsys
    ):
        set_setting(switcher.backup_dir, "ui.showEmail", "false")
        switcher.status()
        out = capsys.readouterr().out
        assert "Account-1 (dev)" in out
        assert "user@example.com" not in out

    @pytest.mark.parametrize("show_email", [True, False])
    def test_map_listing(self, switcher, capsys, show_email: bool):
        from claude_swap.mappings import MappingStore

        MappingStore(switcher.backup_dir).set(
            "/work/proj", "user@example.com", "org-uuid-5678"
        )
        if not show_email:
            set_setting(switcher.backup_dir, "ui.showEmail", "false")
        switcher.list_mappings()
        out = capsys.readouterr().out
        if show_email:
            assert "1: user@example.com [Acme Corp]" in out
            assert "dev" not in out
        else:
            assert "1: dev" in out
            assert "user@example.com" not in out

    @pytest.mark.parametrize("show_email", [True, False])
    def test_add_to_slot_prompt(self, switcher, capsys, show_email: bool):
        if not show_email:
            set_setting(switcher.backup_dir, "ui.showEmail", "false")
        with patch("builtins.input", return_value="n"):
            switcher.add_account_from_token(
                "sk-ant-oat01-xyz", email="new@example.com", slot=1
            )
        out = capsys.readouterr().out
        assert "Slot 1 already occupied" in out
        if show_email:
            assert "user@example.com [Acme Corp]" in out
        else:
            assert "user@example.com" not in out
            assert "Acme Corp" not in out
            assert "\ndev\n" in out

    @pytest.mark.parametrize("show_email", [True, False])
    def test_run_launch_message(
        self, switcher, tmp_path: Path, monkeypatch, capsys, show_email: bool
    ):
        from claude_swap import session as session_mod

        if not show_email:
            set_setting(switcher.backup_dir, "ui.showEmail", "false")

        class Stop(Exception):
            pass

        def fake_exec(self, claude_bin, claude_args, env):
            raise Stop

        monkeypatch.setattr(session_mod.SessionManager, "_exec", fake_exec)
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: "/fake/claude")
        manager = session_mod.SessionManager(switcher)
        monkeypatch.setattr(
            switcher, "resolve_account",
            lambda ident: ("1", "user@example.com", "org-uuid-5678"),
        )
        monkeypatch.setattr(manager, "_ensure_not_api_key", lambda n, e: None)
        monkeypatch.setattr(switcher, "_live_login_identity", lambda: None)
        monkeypatch.setattr(
            manager, "setup_session",
            lambda ident, share, share_history: (tmp_path, "1", "user@example.com"),
        )
        with pytest.raises(Stop):
            manager.run("1", [])
        launching = capsys.readouterr().out
        monkeypatch.setattr(
            switcher, "_live_login_identity",
            lambda: ("user@example.com", "org-uuid-5678"),
        )
        with pytest.raises(Stop):
            manager.run("1", [])
        active = capsys.readouterr().out
        for out in (launching, active):
            assert "Account-1 (" in out
            if show_email:
                assert "Account-1 (user@example.com)" in out
            else:
                assert "Account-1 (dev)" in out
                assert "user@example.com" not in out

    @pytest.mark.parametrize("show_email", [True, False])
    def test_dashboard_pin_row(self, tmp_path: Path, monkeypatch, show_email: bool):
        import types

        from claude_swap.tui import dashboard
        from claude_swap.tui.app import CswapApp

        acc = make_account(1, email=EMAIL, alias="dev")
        app = types.SimpleNamespace(
            switcher=object(),
            snapshot=types.SimpleNamespace(accounts=[acc]),
            show_email=show_email,
        )
        app.account_name = lambda a: CswapApp.account_name(app, a)
        monkeypatch.setattr(dashboard.pin, "is_available", lambda: True)
        monkeypatch.setattr(dashboard.pin, "pinned_identity", lambda sw: (EMAIL, ""))
        monkeypatch.setattr(
            dashboard.DashboardScreen, "app", property(lambda self: app),
            raising=False,
        )
        screen = object.__new__(dashboard.DashboardScreen)
        row = next(
            label for label, action in screen._root_entries()
            if action == "pin-menu"
        )
        assert row.endswith(EMAIL if show_email else "dev")
        assert (EMAIL in row) is show_email

    def test_dashboard_pin_row_unmanaged_pin_keeps_email(
        self, monkeypatch
    ):
        import types

        from claude_swap.tui import dashboard

        app = types.SimpleNamespace(
            switcher=object(), snapshot=None, show_email=False
        )
        monkeypatch.setattr(dashboard.pin, "is_available", lambda: True)
        monkeypatch.setattr(
            dashboard.pin, "pinned_identity", lambda sw: ("other@x.dev", "")
        )
        monkeypatch.setattr(
            dashboard.DashboardScreen, "app", property(lambda self: app),
            raising=False,
        )
        screen = object.__new__(dashboard.DashboardScreen)
        row = next(
            label for label, action in screen._root_entries()
            if action == "pin-menu"
        )
        assert row.endswith("other@x.dev")

    def test_switch_toast_target(self):
        import types

        from claude_swap.tui.app import CswapApp

        snap = types.SimpleNamespace(
            accounts=[make_account(1, email=EMAIL, alias="dev"),
                      make_account(2, email="b@x.dev")]
        )
        to1 = {"number": "1", "email": EMAIL}
        to2 = {"number": "2", "email": "b@x.dev"}
        on = types.SimpleNamespace(show_email=True, snapshot=snap)
        off = types.SimpleNamespace(show_email=False, snapshot=snap)
        assert CswapApp._switched_target(on, to1) == EMAIL
        assert CswapApp._switched_target(off, to1) == "dev"
        assert CswapApp._switched_target(off, to2) == "account 2"
