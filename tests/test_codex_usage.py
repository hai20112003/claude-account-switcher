"""Tests for Codex usage module."""

import json
import urllib.error
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from claude_switcher.codex_usage import (
    CODEX_LOGIN_REQUIRED_USAGE,
    _extract_codex_token,
    fetch_codex_usage,
    fetch_codex_usage_for_account,
    fetch_codex_usage_with_refresh,
    format_codex_usage,
    codex_usage_state,
)

FAKE_CREDS_NESTED = json.dumps({
    "auth_mode": "chatgpt",
    "tokens": {
        "access_token": "sk-test-token",
        "account_id": "acc-123",
    },
})

FAKE_CREDS_FLAT = json.dumps({
    "access_token": "sk-test-token",
    "account_id": "acc-123",
})


class TestExtractCodexToken:
    def test_extracts_token_from_nested_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_NESTED)
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_extracts_token_from_flat_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_FLAT)
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_extracts_token_from_hex_encoded_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_NESTED.encode("utf-8").hex())
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_returns_none_for_invalid_json(self):
        result = _extract_codex_token("not json")
        assert result is None

    def test_returns_none_for_missing_fields(self):
        result = _extract_codex_token('{"other": "data"}')
        assert result is None


class TestFormatCodexUsage:
    def test_formats_both_windows(self):
        reset_primary = (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()
        reset_secondary = (datetime.now(timezone.utc) + timedelta(days=5, hours=13)).timestamp()
        usage = {
            "rate_limit": {
                "primary_window": {"used_percent": 43, "reset_at": reset_primary},
                "secondary_window": {"used_percent": 29, "reset_at": reset_secondary},
            }
        }
        result = format_codex_usage(usage)
        assert "43%" in result
        assert "29%" in result

    def test_returns_unavailable_for_none(self):
        assert format_codex_usage(None) == "Usage unavailable"

    def test_returns_unavailable_for_empty(self):
        assert format_codex_usage({}) == "Usage unavailable"

    def test_returns_login_required_for_expired_session(self):
        assert codex_usage_state(CODEX_LOGIN_REQUIRED_USAGE).display == "Login required"

    def test_usage_state_marks_exhausted_at_100(self):
        usage = {
            "rate_limit": {
                "primary_window": {"used_percent": 100},
                "secondary_window": {"used_percent": 12},
            }
        }
        state = codex_usage_state(usage)
        assert state.available is True
        assert state.is_exhausted() is True
        assert state.max_percent == 100

    def test_usage_state_does_not_mark_99_9_exhausted(self):
        usage = {"rate_limit": {"primary_window": {"used_percent": 99.9}}}
        assert codex_usage_state(usage).is_exhausted() is False


class TestFetchCodexUsageForAccount:
    @patch("claude_switcher.codex_usage.urlopen")
    def test_fetch_tries_second_endpoint_after_first_fails(self, mock_urlopen):
        response_data = json.dumps({"rate_limit": {"primary_window": {"used_percent": 10}}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [urllib.error.URLError("nope"), mock_resp]

        result = fetch_codex_usage(FAKE_CREDS_NESTED)

        assert result["rate_limit"]["primary_window"]["used_percent"] == 10
        assert mock_urlopen.call_count == 2

    @patch("claude_switcher.codex_usage.fetch_codex_usage_with_refresh")
    @patch("claude_switcher.codex_usage.keychain")
    def test_fetches_for_account(self, mock_kc, mock_fetch):
        mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
        mock_fetch.return_value = ({"rate_limit": {}}, None)
        result = fetch_codex_usage_for_account("user@test.com")
        mock_kc.read_credentials.assert_called_with("codex-switcher:user@test.com")
        assert result is not None

    @patch("claude_switcher.codex_usage.keychain")
    def test_returns_none_when_no_creds(self, mock_kc):
        mock_kc.read_credentials.return_value = None
        result = fetch_codex_usage_for_account("user@test.com")
        assert result is None

    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    def test_fetch_refreshes_stale_credentials(self, mock_refresh, mock_fetch_once):
        refreshed = json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "fresh", "account_id": "acc-123"},
        })
        mock_fetch_once.side_effect = [None, {"rate_limit": {}}]
        mock_refresh.return_value = refreshed

        usage, refreshed_creds = fetch_codex_usage_with_refresh(FAKE_CREDS_NESTED)

        assert usage == {"rate_limit": {}}
        assert refreshed_creds == refreshed

    @patch("claude_switcher.codex_usage.fetch_codex_usage_with_refresh")
    @patch("claude_switcher.codex_usage.keychain")
    def test_fetch_for_account_saves_refreshed_credentials(self, mock_kc, mock_fetch):
        mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
        mock_fetch.return_value = ({"rate_limit": {}}, '{"fresh": true}')

        assert fetch_codex_usage_for_account("user@test.com") == {"rate_limit": {}}
        mock_kc.write_credentials.assert_called_once_with(
            "codex-switcher:user@test.com",
            "user@test.com",
            '{"fresh": true}',
        )
