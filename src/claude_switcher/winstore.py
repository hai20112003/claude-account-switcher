"""Windows credential storage.

Claude Code on Windows keeps its OAuth session in ``~/.claude/.credentials.json``
instead of a Keychain. That file also holds MCP server tokens (``mcpOAuth``), which
belong to the machine rather than to a Claude account, so switching only swaps the
``claudeAiOauth`` entry and leaves every other key untouched.

Saved account backups are encrypted with DPAPI (CurrentUser scope), so only the same
Windows user on the same machine can decrypt them.
"""

import ctypes
import getpass
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

CLAUDE_OAUTH_KEY = "claudeAiOauth"
# Another process (Claude Code itself) may hold the file open for a moment.
_REPLACE_ATTEMPTS = 5


def claude_credentials_file() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".claude"
    return base / ".credentials.json"


def vault_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "claude-switcher" / "vault"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.2)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- DPAPI -----------------------------------------------------------------

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob_in(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _blob_out(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def dpapi_protect(data: bytes) -> bytes:
    blob, _buffer = _blob_in(data)
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob), "claude-switcher", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise RuntimeError("Windows could not encrypt the saved credentials (DPAPI).")
    return _blob_out(out)


def dpapi_unprotect(data: bytes) -> bytes:
    blob, _buffer = _blob_in(data)
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise RuntimeError("Windows could not decrypt the saved credentials (DPAPI).")
    return _blob_out(out)


# --- Active Claude Code session ---------------------------------------------

def _read_credentials_file() -> dict | None:
    try:
        data = json.loads(claude_credentials_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def read_active() -> str | None:
    data = _read_credentials_file()
    if not data or not data.get(CLAUDE_OAUTH_KEY):
        return None
    return json.dumps({CLAUDE_OAUTH_KEY: data[CLAUDE_OAUTH_KEY]})


def write_active(creds: str) -> None:
    try:
        incoming = json.loads(creds)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Saved Claude credentials are not valid JSON.") from exc
    if not isinstance(incoming, dict) or not incoming.get(CLAUDE_OAUTH_KEY):
        raise RuntimeError("Saved Claude credentials have no claudeAiOauth session.")
    data = _read_credentials_file() or {}
    data[CLAUDE_OAUTH_KEY] = incoming[CLAUDE_OAUTH_KEY]
    _atomic_write(claude_credentials_file(), json.dumps(data).encode("utf-8"))


def delete_active() -> bool:
    data = _read_credentials_file()
    if not data or CLAUDE_OAUTH_KEY not in data:
        return False
    del data[CLAUDE_OAUTH_KEY]
    _atomic_write(claude_credentials_file(), json.dumps(data).encode("utf-8"))
    return True


def active_account() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "windows"


# --- Saved account backups ---------------------------------------------------

def _vault_file(service: str) -> Path:
    name = hashlib.sha256(service.encode("utf-8")).hexdigest()
    return vault_dir() / f"{name}.bin"


def _read_entry(service: str, unprotect=None) -> dict | None:
    unprotect = unprotect or dpapi_unprotect
    try:
        raw = _vault_file(service).read_bytes()
        entry = json.loads(unprotect(raw).decode("utf-8"))
    except (FileNotFoundError, OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("service") != service:
        return None
    return entry


def read_saved(service: str, unprotect=None) -> str | None:
    entry = _read_entry(service, unprotect)
    secret = entry.get("secret") if entry else None
    return secret if isinstance(secret, str) and secret else None


def saved_account(service: str, unprotect=None) -> str | None:
    entry = _read_entry(service, unprotect)
    account = entry.get("account") if entry else None
    return account if isinstance(account, str) else None


def write_saved(service: str, account: str, secret: str, protect=None) -> None:
    protect = protect or dpapi_protect
    payload = json.dumps({"service": service, "account": account, "secret": secret}).encode("utf-8")
    _atomic_write(_vault_file(service), protect(payload))


def delete_saved(service: str) -> bool:
    try:
        _vault_file(service).unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True
