"""Tests for the Windows credential store."""

import json
import sys

import pytest

import claude_switcher.keychain as keychain_mod
from claude_switcher import winstore

OAUTH = {"accessToken": "sk-ant-oat01-a", "refreshToken": "sk-ant-ort01-a", "expiresAt": 1}
OTHER_OAUTH = {"accessToken": "sk-ant-oat01-b", "refreshToken": "sk-ant-ort01-b", "expiresAt": 2}
MCP = {"slack|1": {"accessToken": "xoxp-mcp"}}


def _fake_protect(data: bytes) -> bytes:
    return b"ENC:" + data[::-1]


def _fake_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"ENC:"):
        raise RuntimeError("bad blob")
    return data[4:][::-1]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(winstore, "dpapi_protect", _fake_protect)
    monkeypatch.setattr(winstore, "dpapi_unprotect", _fake_unprotect)
    return tmp_path


def _write_credentials_file(data: dict) -> None:
    path = winstore.claude_credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_credentials_file() -> dict:
    return json.loads(winstore.claude_credentials_file().read_text(encoding="utf-8"))


class TestActiveSession:
    def test_read_returns_only_the_claude_session(self, store):
        _write_credentials_file({"claudeAiOauth": OAUTH, "mcpOAuth": MCP})
        assert json.loads(winstore.read_active()) == {"claudeAiOauth": OAUTH}

    def test_read_missing_file_or_session(self, store):
        assert winstore.read_active() is None
        _write_credentials_file({"mcpOAuth": MCP})
        assert winstore.read_active() is None

    def test_write_swaps_session_and_keeps_mcp_tokens(self, store):
        _write_credentials_file({"claudeAiOauth": OAUTH, "mcpOAuth": MCP})
        winstore.write_active(json.dumps({"claudeAiOauth": OTHER_OAUTH}))
        assert _read_credentials_file() == {"claudeAiOauth": OTHER_OAUTH, "mcpOAuth": MCP}

    def test_write_creates_file_when_missing(self, store):
        winstore.write_active(json.dumps({"claudeAiOauth": OAUTH}))
        assert _read_credentials_file() == {"claudeAiOauth": OAUTH}

    def test_write_rejects_blob_without_session(self, store):
        _write_credentials_file({"claudeAiOauth": OAUTH})
        with pytest.raises(RuntimeError):
            winstore.write_active(json.dumps({"mcpOAuth": MCP}))
        with pytest.raises(RuntimeError):
            winstore.write_active("not json")
        assert _read_credentials_file() == {"claudeAiOauth": OAUTH}

    def test_delete_removes_only_the_session_once(self, store):
        _write_credentials_file({"claudeAiOauth": OAUTH, "mcpOAuth": MCP})
        assert winstore.delete_active() is True
        assert winstore.delete_active() is False
        assert _read_credentials_file() == {"mcpOAuth": MCP}


class TestSavedBackups:
    def test_round_trip_is_encrypted_on_disk(self, store):
        winstore.write_saved("claude-switcher:a@b.com", "hainv", "secret-blob")
        files = list(winstore.vault_dir().glob("*.bin"))
        assert len(files) == 1
        assert b"secret-blob" not in files[0].read_bytes()
        assert b"a@b.com" not in files[0].name.encode()
        assert winstore.read_saved("claude-switcher:a@b.com") == "secret-blob"
        assert winstore.saved_account("claude-switcher:a@b.com") == "hainv"

    def test_overwrite_and_delete(self, store):
        winstore.write_saved("codex-switcher:a@b.com", "a@b.com", "v1")
        winstore.write_saved("codex-switcher:a@b.com", "a@b.com", "v2")
        assert winstore.read_saved("codex-switcher:a@b.com") == "v2"
        assert winstore.delete_saved("codex-switcher:a@b.com") is True
        assert winstore.delete_saved("codex-switcher:a@b.com") is False
        assert winstore.read_saved("codex-switcher:a@b.com") is None

    def test_unreadable_backup_returns_none(self, store):
        winstore.write_saved("claude-switcher:a@b.com", "hainv", "secret")
        path = next(winstore.vault_dir().glob("*.bin"))
        path.write_bytes(b"garbage")
        assert winstore.read_saved("claude-switcher:a@b.com") is None


class TestKeychainDispatch:
    @pytest.fixture(autouse=True)
    def windows_backend(self, store, monkeypatch):
        monkeypatch.setattr(keychain_mod, "BACKEND", "windows")

    def test_active_service_maps_to_credentials_file(self):
        _write_credentials_file({"claudeAiOauth": OAUTH, "mcpOAuth": MCP})
        creds = keychain_mod.read_credentials(keychain_mod.CLAUDE_SERVICE)
        assert json.loads(creds) == {"claudeAiOauth": OAUTH}

        keychain_mod.write_credentials(
            keychain_mod.CLAUDE_SERVICE, "hainv", json.dumps({"claudeAiOauth": OTHER_OAUTH})
        )
        assert _read_credentials_file()["mcpOAuth"] == MCP

        assert keychain_mod.delete_credentials(keychain_mod.CLAUDE_SERVICE) is True
        assert keychain_mod.delete_credentials(keychain_mod.CLAUDE_SERVICE) is False
        assert keychain_mod.read_account_attribute(keychain_mod.CLAUDE_SERVICE)

    def test_backup_services_map_to_vault(self):
        keychain_mod.write_credentials("claude-switcher:a@b.com", "hainv", "blob")
        assert keychain_mod.read_credentials("claude-switcher:a@b.com") == "blob"
        assert keychain_mod.read_account_attribute("claude-switcher:a@b.com") == "hainv"
        assert keychain_mod.delete_credentials("claude-switcher:a@b.com") is True


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_real_dpapi_round_trip():
    secret = "xin chào".encode("utf-8") * 200
    protected = winstore.dpapi_protect(secret)
    assert protected != secret
    assert winstore.dpapi_unprotect(protected) == secret
