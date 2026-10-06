"""Settings in a config file, the password in the keyring (or a 0600 file).

The password goes to the desktop Secret Service through libsecret. Without
a usable keyring it falls back to an owner-only file next to the config.
It never appears in logs, the manifest, D-Bus replies or command lines.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from blueferry_webdav import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.webdav.Password"
MAX_FILE_BYTES = 16 * 1024
DEFAULT_FOLDER = "BlueFerry"
DEFAULT_MAX_SIZE_MB = 2048


class SettingsError(Exception):
    pass


def config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(config_home) / "blueferry" / "plugins" / PLUGIN_ID


@dataclass(frozen=True, slots=True)
class Settings:
    url: str
    username: str
    folder: str = DEFAULT_FOLDER
    public_link: bool = False
    max_size_mb: int = DEFAULT_MAX_SIZE_MB
    allow_http_lan: bool = False
    web_url: str = ""
    nextcloud: bool = False   # detected when the settings were saved
    key_store: str = "keyring"  # "keyring" or "file"

    @property
    def max_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SettingsError("config directory has the wrong owner or type")
    path.chmod(0o700)
    return path


def _write_private(path: Path, text: str) -> None:
    _private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _read_private(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SettingsError(f"{path.name} has the wrong owner or type")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SettingsError(f"{path.name} is readable by other users")
        text = stream.read(MAX_FILE_BYTES + 1)
    if len(text) > MAX_FILE_BYTES:
        raise SettingsError(f"{path.name} is too large")
    return text


def _typed(raw: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    value = raw.get(key, default)
    if kind is int and isinstance(value, bool):
        return default
    return value if isinstance(value, kind) else default


class SettingsStore:
    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        self.directory = directory or config_dir()
        self._secret = secret  # gi.repository.Secret, injectable for tests

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def key_path(self) -> Path:
        return self.directory / "password"

    def load(self) -> Settings | None:
        try:
            raw = json.loads(_read_private(self.config_path))
        except FileNotFoundError:
            return None
        except ValueError:
            raise SettingsError("config.json is not valid JSON") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
            raise SettingsError("config.json has no WebDAV address")
        return Settings(
            url=raw["url"],
            username=_typed(raw, "username", str, ""),
            folder=_typed(raw, "folder", str, DEFAULT_FOLDER),
            public_link=_typed(raw, "public_link", bool, False),
            max_size_mb=max(1, _typed(raw, "max_size_mb", int, DEFAULT_MAX_SIZE_MB)),
            allow_http_lan=_typed(raw, "allow_http_lan", bool, False),
            web_url=_typed(raw, "web_url", str, ""),
            nextcloud=_typed(raw, "nextcloud", bool, False),
            key_store="file" if raw.get("key_store") == "file" else "keyring",
        )

    def save(self, settings: Settings, password: str, *, prefer_keyring: bool = True) -> str:
        """Store the password (keyring first) and the config; return the store."""
        store = "file"
        if prefer_keyring and self._store_keyring(settings, password):
            store = "keyring"
            self.key_path.unlink(missing_ok=True)
        else:
            _write_private(self.key_path, password + "\n")
        values = asdict(settings)
        values["key_store"] = store
        _write_private(self.config_path, json.dumps(values, indent=2) + "\n")
        return store

    def password(self, settings: Settings) -> str:
        if settings.key_store == "file":
            try:
                value = _read_private(self.key_path).rstrip("\n")
            except FileNotFoundError:
                raise SettingsError("the password file is missing; enter it again") from None
        else:
            value = self._lookup_keyring(settings)
        if not value:
            raise SettingsError("no password stored; enter it in the plugin settings")
        return value

    def forget(self) -> None:
        settings = None
        try:
            settings = self.load()
        except SettingsError:
            pass
        if settings is not None and settings.key_store == "keyring":
            self.forget_keyring(settings)
        self.key_path.unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)

    def forget_keyring(self, settings: Settings) -> None:
        secret = self._module()
        if secret is None:
            return
        try:
            secret.password_clear_sync(self._schema(secret), self._attributes(settings), None)
        except Exception:  # nosec B110 - best effort
            pass

    # ---- libsecret -------------------------------------------------------------

    def _module(self) -> Any:
        if self._secret is not None:
            return self._secret
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError):
            return None
        self._secret = Secret
        return Secret

    @staticmethod
    def _schema(secret: Any) -> Any:
        return secret.Schema.new(SCHEMA, secret.SchemaFlags.NONE, {
            "server": secret.SchemaAttributeType.STRING,
            "user": secret.SchemaAttributeType.STRING,
        })

    @staticmethod
    def _attributes(settings: Settings) -> dict[str, str]:
        return {"server": settings.url, "user": settings.username}

    def _store_keyring(self, settings: Settings, password: str) -> bool:
        secret = self._module()
        if secret is None:
            return False
        try:
            return bool(secret.password_store_sync(
                self._schema(secret), self._attributes(settings), secret.COLLECTION_DEFAULT,
                "BlueFerry WebDAV password", password, None,
            ))
        except Exception:
            return False

    def _lookup_keyring(self, settings: Settings) -> str:
        secret = self._module()
        if secret is None:
            raise SettingsError("no Secret Service client is installed")
        try:
            value = secret.password_lookup_sync(
                self._schema(secret), self._attributes(settings), None,
            )
        except Exception:
            raise SettingsError("the desktop keyring is locked or unavailable") from None
        return str(value or "")
