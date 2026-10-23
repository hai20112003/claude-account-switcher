"""Tests for the UI-independent controller, including a full Windows switch flow."""

import json
from unittest.mock import patch

import pytest

import claude_switcher.keychain as keychain_mod
from claude_switcher import winstore
from claude_switcher.config import AccountInfo, add_account, get_active_account, set_auto_switch_enabled
from claude_switcher.controller import SwitcherController
from claude_switcher.usage_state import UsageState, UsageWindow

A = {"accessToken": "tok-a", "refreshToken": "ref-a", "expiresAt": 1}
B = {"accessToken": "tok-b", "refreshToken": "ref-b", "expiresAt": 2}
MCP = {"slack|1": {"accessToken": "xoxp"}}


def _fake_protect(data: bytes) -> bytes:
    return b"ENC:" + data[::-1]


def _fake_unprotect(data: bytes) -> bytes:
    return data[4:][::-1]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(winstore, "dpapi_protect", _fake_protect)
    monkeypatch.setattr(winstore, "dpapi_unprotect", _fake_unprotect)
    monkeypatch.setattr(keychain_mod, "BACKEND", "windows")
    monkeypatch.setattr("claude_switcher.core.CLAUDE_STATE_FILE", tmp_path / ".claude.json")
    (tmp_path / ".claude.json").write_text(
        json.dumps({"theme": "dark", "oauthAccount": {"emailAddress": "a@x.com"}}), encoding="utf-8"
    )
    return tmp_path


def _seed_two_accounts(sandbox):
    config = sandbox / "accounts.json"
    creds_file = winstore.claude_credentials_file()
    creds_file.parent.mkdir(parents=True)
    creds_file.write_text(json.dumps({"claudeAiOauth": A, "mcpOAuth": MCP}), encoding="utf-8")
    winstore.write_saved("claude-switcher:a@x.com", "hainv", json.dumps({"claudeAiOauth": A}))
    winstore.write_saved("claude-switcher:b@x.com", "hainv", json.dumps({"claudeAiOauth": B}))
    add_account(AccountInfo("a@x.com", "max", "", True, "hainv", {"emailAddress": "a@x.com"}), config)
    add_account(AccountInfo("b@x.com", "pro", "", False, "hainv", {"emailAddress": "b@x.com"}), config)
    return config


def _controller(config, events):
    return SwitcherController(
        notify=lambda title, message: events.append(("notify", title, message)),
        alert=lambda title, message: events.append(("alert", title, message)),
        on_change=lambda: events.append(("change",)),
        config_path=config,
        run_in_background=lambda fn: fn(),
    )


@patch("claude_switcher.controller.fetch_usage_for_account", return_value=None)
@patch("claude_switcher.controller.fetch_active_usage", return_value=None)
def test_switch_swaps_session_keeps_mcp_and_backs_up_refreshed_token(_active, _saved, sandbox):
    config = _seed_two_accounts(sandbox)
    creds_file = winstore.claude_credentials_file()
    refreshed_a = dict(A, accessToken="tok-a-refreshed")
    creds_file.write_text(json.dumps({"claudeAiOauth": refreshed_a, "mcpOAuth": MCP}), encoding="utf-8")
    events = []

    _controller(config, events).switch("claude", "b@x.com")

    assert json.loads(creds_file.read_text(encoding="utf-8")) == {"claudeAiOauth": B, "mcpOAuth": MCP}
    assert json.loads(winstore.read_saved("claude-switcher:a@x.com")) == {"claudeAiOauth": refreshed_a}
    state = json.loads((sandbox / ".claude.json").read_text(encoding="utf-8"))
    assert state == {"theme": "dark", "oauthAccount": {"emailAddress": "b@x.com"}}
    assert get_active_account(config).email == "b@x.com"
    assert ("notify", "Claude Code account switched", "b@x.com") in events
    assert not [e for e in events if e[0] == "alert"]


def test_switch_to_active_account_does_nothing(sandbox):
    config = _seed_two_accounts(sandbox)
    events = []
    _controller(config, events).switch("claude", "a@x.com")
    assert events == []


def test_switch_without_backup_alerts_and_keeps_session(sandbox):
    config = _seed_two_accounts(sandbox)
    winstore.delete_saved("claude-switcher:b@x.com")
    events = []

    with patch("claude_switcher.controller.SwitcherController.refresh_usage"):
        _controller(config, events).switch("claude", "b@x.com")

    assert json.loads(winstore.read_active()) == {"claudeAiOauth": A}
    assert events[0][0] == "alert"
    assert get_active_account(config).email == "a@x.com"


def test_remove_active_account_is_refused(sandbox):
    config = _seed_two_accounts(sandbox)
    events = []
    _controller(config, events).remove_account("claude", "a@x.com")
    assert events[0][:2] == ("alert", "Cannot remove")
    assert winstore.read_saved("claude-switcher:a@x.com")


def test_remove_inactive_account_deletes_backup(sandbox):
    config = _seed_two_accounts(sandbox)
    events = []
    controller = _controller(config, events)
    controller.remove_account("claude", "b@x.com")
    assert winstore.read_saved("claude-switcher:b@x.com") is None
    assert [a.email for a in controller.accounts("claude")] == ["a@x.com"]


def test_has_credentials_and_usage_placeholder(sandbox):
    config = _seed_two_accounts(sandbox)
    controller = _controller(config, [])
    accounts = controller.accounts("claude")
    assert all(controller.has_credentials(a) for a in accounts)
    assert controller.usage_text(accounts[0]) == "•••"


@patch("claude_switcher.controller.fetch_usage_for_account")
@patch("claude_switcher.controller.fetch_active_usage")
def test_refresh_auto_switches_when_active_account_is_full(mock_active, mock_saved, sandbox):
    config = _seed_two_accounts(sandbox)
    set_auto_switch_enabled("claude", True, config)
    mock_active.return_value = {"five_hour": {"utilization": 100}, "seven_day": {"utilization": 40}}
    mock_saved.return_value = {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 5}}
    events = []
    controller = _controller(config, events)

    with patch.object(SwitcherController, "refresh_usage", wraps=controller.refresh_usage) as again:
        controller._refresh_usage_now()

    assert get_active_account(config).email == "b@x.com"
    assert json.loads(winstore.read_active()) == {"claudeAiOauth": B}
    assert ("notify", "Auto-switched Claude Code", "b@x.com") in events
    again.assert_called()


def test_busy_provider_blocks_a_second_action(sandbox):
    config = _seed_two_accounts(sandbox)
    events = []
    controller = _controller(config, events)
    assert controller._claim("claude")
    controller.switch("claude", "b@x.com")
    assert events[0][1] == "Claude Code is busy"
    assert get_active_account(config).email == "a@x.com"


def test_usage_state_for_full_account_is_exhausted():
    state = UsageState(True, "5h 100%", (UsageWindow("5h", 100.0),))
    assert state.is_exhausted(100.0)
