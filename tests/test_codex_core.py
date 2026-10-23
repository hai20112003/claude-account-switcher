"""Tests for Codex CLI business logic."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import claude_switcher.codex_core as codex_core_mod
from claude_switcher.codex_core import (
    CodexCredentialsExpiredError,
    CODEX_KEYRING_UNSUPPORTED_MESSAGE,
    add_new_codex_account,
    backup_codex_credentials,
    check_codex_cli,
    get_codex_auth_status,
    read_codex_credentials,
    import_current_codex_account,
    normalize_codex_credentials_blob,
    refresh_codex_credentials,
    run_codex_login,
    switch_codex_account,
    remove_codex_account,
)
from claude_switcher.config import AccountInfo, load_accounts, save_accounts


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.sig"


def _auth_json(email="user@test.com", plan="plus"):
    return json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "sk-test",
            "account_id": "acc-123",
            "refresh_token": "refresh",
            "id_token": _jwt({
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": plan,
                    "chatgpt_account_id": "acc-123",
                },
            }),
        },
    })


class TestCodexCLI:
    @patch("claude_switcher.codex_core.shutil.which", return_value="/usr/local/bin/codex")
    def test_check_cli_found(self, mock_which):
        assert check_codex_cli() is True

    @patch("claude_switcher.codex_core.Path.is_file", return_value=False)
    @patch("claude_switcher.codex_core.shutil.which", return_value=None)
    def test_check_cli_not_found_in_path(self, mock_which, mock_is_file):
        assert check_codex_cli() is False

    @patch("claude_switcher.codex_core.subprocess.run")
    def test_get_auth_status_logged_in(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        status = get_codex_auth_status()
        assert status is not None
        assert status["loggedIn"] is True

    @patch("claude_switcher.codex_core.subprocess.run")
    def test_get_auth_status_not_logged_in(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        assert get_codex_auth_status() is None


class TestCodexCredentials:
    def test_normalize_plain_json_credentials(self):
        creds = _auth_json(email="plain@test.com")
        assert normalize_codex_credentials_blob(creds) == creds

    def test_normalize_hex_encoded_json_credentials(self):
        creds = _auth_json(email="hex@test.com")
        encoded = creds.encode("utf-8").hex()
        assert normalize_codex_credentials_blob(encoded) == creds

    def test_normalize_leaves_invalid_text_unchanged(self):
        assert normalize_codex_credentials_blob("not json") == "not json"

    def test_read_from_auth_file(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text('{"token": "sk-test-123"}')
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            creds = read_codex_credentials()

        assert creds is not None
        assert "sk-test-123" in creds

    def test_read_missing_file_returns_none_in_file_mode(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            assert read_codex_credentials() is None

    def test_keyring_mode_raises_clear_error(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "keyring"')

        with patch.object(codex_core_mod, "CODEX_CONFIG_FILE", config_file):
            with pytest.raises(RuntimeError, match="keyring credential storage"):
                read_codex_credentials()
        assert "cli_auth_credentials_store" in CODEX_KEYRING_UNSUPPORTED_MESSAGE

    @patch("claude_switcher.codex_core.urlopen")
    def test_refresh_credentials_updates_rotated_tokens(self, mock_urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": _jwt({"email": "user@test.com"}),
            "expires_in": 3600,
        }).encode()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = response

        refreshed = refresh_codex_credentials(_auth_json(email="user@test.com"))
        data = json.loads(refreshed)

        assert data["tokens"]["access_token"] == "new-access"
        assert data["tokens"]["refresh_token"] == "new-refresh"
        assert "last_refresh" in data

    @patch("claude_switcher.codex_core.urlopen")
    def test_refresh_credentials_raises_for_consumed_refresh_token(self, mock_urlopen):
        error_body = json.dumps({
            "error": {"message": "Your refresh token has already been used."}
        }).encode()
        mock_urlopen.side_effect = codex_core_mod.HTTPError(
            "https://auth.openai.com/oauth/token",
            401,
            "Unauthorized",
            {},
            MagicMock(read=MagicMock(return_value=error_body)),
        )

        with pytest.raises(CodexCredentialsExpiredError):
            refresh_codex_credentials(_auth_json(email="user@test.com"))


class TestBackupCodexCredentials:
    @patch("claude_switcher.codex_core.keychain")
    def test_backup_writes_blob_to_keychain(self, mock_kc):
        creds = _auth_json(email="user@test.com")

        assert backup_codex_credentials(creds) == "user@test.com"

        mock_kc.write_credentials.assert_called_once_with(
            "codex-switcher:user@test.com", "user@test.com", creds
        )

    @patch("claude_switcher.codex_core.keychain")
    def test_backup_skips_credentials_without_email(self, mock_kc):
        assert backup_codex_credentials(json.dumps({"tokens": {}})) is None
        mock_kc.write_credentials.assert_not_called()


class TestImportCodexAccount:
    @patch("claude_switcher.codex_core.keychain")
    @patch("claude_switcher.codex_core.get_codex_auth_status")
    def test_import_success(self, mock_status, mock_kc, tmp_path):
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text(_auth_json(email="user@test.com", plan="pro"))
        config_file.write_text('cli_auth_credentials_store = "file"')
        mock_status.return_value = {"loggedIn": True}

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            result = import_current_codex_account(tmp_path / "accounts.json")

        assert result is not None
        assert result.email == "user@test.com"
        assert result.subscription_type == "pro"
        assert result.provider == "codex"
        mock_kc.write_credentials.assert_called_once()

    def test_import_no_credentials(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            assert import_current_codex_account(tmp_path / "accounts.json") is None

    def test_import_no_auto_credentials_returns_none(self, tmp_path):
        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", tmp_path / "missing.toml"
        ):
            assert import_current_codex_account(tmp_path / "accounts.json") is None


class TestCodexLogin:
    @patch("claude_switcher.codex_core._launch_codex_login_terminal")
    @patch("claude_switcher.codex_core.time.sleep")
    def test_run_codex_login_waits_for_auth_file(self, mock_sleep, mock_launch):
        with patch(
            "claude_switcher.codex_core._read_codex_credentials_from_file",
            side_effect=[None, _auth_json(email="new@test.com")],
        ):
            assert run_codex_login(timeout=3) is True
        mock_launch.assert_called_once()

    @patch("claude_switcher.codex_core.IS_WINDOWS", False)
    @patch("claude_switcher.codex_core.subprocess.run")
    @patch("claude_switcher.codex_core._codex_cmd", return_value="/opt/homebrew/bin/codex")
    def test_launch_codex_login_opens_terminal(self, mock_cmd, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("claude_switcher.codex_core.tempfile.gettempdir", return_value=str(tmp_path)):
            codex_core_mod._launch_codex_login_terminal()

        script_path = tmp_path / "claude-switcher-codex-login-{}.command".format(
            codex_core_mod.os.getpid()
        )
        assert script_path.exists()
        assert 'cli_auth_credentials_store="file"' in script_path.read_text()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][0] == "open"

    @patch("claude_switcher.codex_core.IS_WINDOWS", True)
    @patch("claude_switcher.codex_core.subprocess.CREATE_NEW_CONSOLE", 0x10, create=True)
    @patch("claude_switcher.codex_core.subprocess.Popen")
    @patch("claude_switcher.codex_core._codex_cmd", return_value=r"C:\npm\codex.cmd")
    def test_launch_codex_login_opens_console_on_windows(self, mock_cmd, mock_popen, tmp_path):
        with patch("claude_switcher.codex_core.tempfile.gettempdir", return_value=str(tmp_path)):
            codex_core_mod._launch_codex_login_terminal()

        script_path = tmp_path / "claude-switcher-codex-login-{}.cmd".format(codex_core_mod.os.getpid())
        script = script_path.read_bytes().decode("utf-8")
        assert r'"C:\npm\codex.cmd" login -c "cli_auth_credentials_store=\"file\""' in script
        assert "\r\n" in script
        assert mock_popen.call_args.args[0] == ["cmd.exe", "/c", str(script_path)]
        assert mock_popen.call_args.kwargs["creationflags"] == 0x10

    @patch("claude_switcher.codex_core.import_current_codex_account")
    @patch("claude_switcher.codex_core.run_codex_login")
    @patch("claude_switcher.codex_core.run_codex_logout")
    def test_add_new_account_opens_login_when_no_current_file(
        self, mock_logout, mock_login, mock_import, tmp_path
    ):
        config = tmp_path / "accounts.json"
        mock_login.return_value = True
        mock_import.return_value = AccountInfo(
            "new@test.com", "plus", "", True, "new@test.com", provider="codex"
        )

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", tmp_path / "missing.toml"
        ):
            result = add_new_codex_account(config)

        assert result.email == "new@test.com"
        mock_logout.assert_called_once()
        mock_login.assert_called_once()

    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.run_codex_login")
    @patch("claude_switcher.codex_core.run_codex_logout")
    @patch("claude_switcher.codex_core.keychain")
    def test_add_new_account_restores_saved_creds_when_cancelled(
        self, mock_kc, mock_logout, mock_login, mock_write, tmp_path
    ):
        config = tmp_path / "accounts.json"
        config_file = tmp_path / "config.toml"
        auth_file = tmp_path / "auth.json"
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old@test.com", provider="codex")
        ], config)
        mock_kc.read_credentials.return_value = _auth_json(email="old@test.com")
        mock_login.return_value = False

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            result = add_new_codex_account(config)

        assert result is None
        mock_write.assert_called_once_with(_auth_json(email="old@test.com"))


class TestSwitchCodexAccount:
    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.keychain")
    def test_switch_saves_current_loads_target(self, mock_kc, mock_write, tmp_path):
        config = tmp_path / "accounts.json"
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text('{"token": "old-token"}')
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        mock_kc.read_credentials.return_value = '{"token": "target-token"}'

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            switch_codex_account("new@test.com", config)

        mock_kc.write_credentials.assert_any_call(
            "codex-switcher:old@test.com", "old", '{"token": "old-token"}'
        )
        mock_write.assert_called_once_with('{"token": "target-token"}')
        active = [a for a in load_accounts(config) if a.provider == "codex" and a.active]
        assert active[0].email == "new@test.com"

    @patch("claude_switcher.codex_core.refresh_codex_credentials")
    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.keychain")
    def test_switch_refreshes_target_before_write(self, mock_kc, mock_write, mock_refresh, tmp_path):
        config = tmp_path / "accounts.json"
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text(_auth_json(email="old@test.com"))
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        target_creds = _auth_json(email="new@test.com")
        refreshed_creds = _auth_json(email="new@test.com", plan="pro")
        mock_kc.read_credentials.return_value = target_creds
        mock_refresh.return_value = refreshed_creds

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            switch_codex_account("new@test.com", config)

        mock_write.assert_called_once_with(refreshed_creds)
        mock_kc.write_credentials.assert_any_call("codex-switcher:new@test.com", "new", refreshed_creds)

    @patch("claude_switcher.codex_core.refresh_codex_credentials")
    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.keychain")
    def test_switch_refuses_expired_target_session(self, mock_kc, mock_write, mock_refresh, tmp_path):
        config = tmp_path / "accounts.json"
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text(_auth_json(email="old@test.com"))
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        mock_kc.read_credentials.return_value = _auth_json(email="new@test.com")
        mock_refresh.side_effect = CodexCredentialsExpiredError("expired")

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            with pytest.raises(RuntimeError, match="expired"):
                switch_codex_account("new@test.com", config)

        mock_write.assert_not_called()

    @patch("claude_switcher.codex_core.keychain")
    def test_switch_decodes_hex_encoded_target_credentials(self, mock_kc, tmp_path):
        config = tmp_path / "accounts.json"
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        target_creds = _auth_json(email="new@test.com")
        auth_file.write_text(_auth_json(email="old@test.com"))
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        mock_kc.read_credentials.return_value = target_creds.encode("utf-8").hex()

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            switch_codex_account("new@test.com", config)

        assert json.loads(auth_file.read_text(encoding="utf-8"))["tokens"]["account_id"] == "acc-123"
        assert auth_file.read_text(encoding="utf-8") == target_creds

    @patch("claude_switcher.codex_core.keychain")
    def test_switch_missing_keychain_raises(self, mock_kc, tmp_path):
        config = tmp_path / "accounts.json"
        save_accounts([AccountInfo("new@test.com", "plus", "", False, "new", provider="codex")], config)
        mock_kc.read_credentials.return_value = None

        with pytest.raises(RuntimeError, match="Credentials not found"):
            switch_codex_account("new@test.com", config)


class TestRemoveCodexAccount:
    @patch("claude_switcher.codex_core.keychain")
    def test_remove_account(self, mock_kc, tmp_path):
        config = tmp_path / "accounts.json"
        save_accounts([AccountInfo("rm@test.com", "plus", "", False, "rm", provider="codex")], config)

        remove_codex_account("rm@test.com", config)

        mock_kc.delete_credentials.assert_called_with("codex-switcher:rm@test.com")
        assert load_accounts(config) == []
