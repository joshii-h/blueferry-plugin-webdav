"""Settings in a config file, the password in the keyring (or a 0600 file).

The password goes to the desktop Secret Service through libsecret. Without
a usable keyring it falls back to an owner-only file next to the config.
It never appears in logs, the manifest, D-Bus replies or command lines.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from blueferry_plugin_kit import secrets as _secrets
from blueferry_plugin_kit.secrets import KeyringStore, read_private_text, write_private

from blueferry_webdav import PLUGIN_ID

SCHEMA = "io.weirdware.blueferry.webdav.Password"
DEFAULT_FOLDER = "BlueFerry"
DEFAULT_MAX_SIZE_MB = 2048



class SettingsError(_secrets.SecretsError):
    """The settings or the stored password are unusable; names no secret.

    A subclass of the kit's ``SecretsError`` (so either name catches it);
    the store re-raises the kit's errors under this name, so logs and the
    base service's "plugin call failed: …" say ``SettingsError``.
    """


def config_dir() -> Path:
    return _secrets.config_dir(PLUGIN_ID)


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


def _typed(raw: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    value = raw.get(key, default)
    if kind is int and isinstance(value, bool):
        return default
    return value if isinstance(value, kind) else default


class SettingsStore(KeyringStore):
    SECRET_SCHEMA = SCHEMA
    SECRET_ATTRIBUTES = ("server", "user")
    SECRET_LABEL = "BlueFerry WebDAV password"

    def __init__(self, directory: Path | None = None, *, secret: Any = None) -> None:
        # secret: gi.repository.Secret, injectable for tests
        super().__init__(directory or config_dir(), secret=secret)

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def key_path(self) -> Path:
        return self.directory / "password"

    def load(self) -> Settings | None:
        try:
            raw = json.loads(read_private_text(self.config_path))
        except FileNotFoundError:
            return None
        except ValueError:
            raise SettingsError("config.json is not valid JSON") from None
        except _secrets.SecretsError as error:
            raise SettingsError(str(error)) from None
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
        try:
            store = self.save_secret(
                self._attributes(settings), password, prefer_keyring=prefer_keyring,
            )
            values = asdict(settings)
            values["key_store"] = store
            write_private(self.config_path, json.dumps(values, indent=2) + "\n")
        except SettingsError:
            raise
        except _secrets.SecretsError as error:
            raise SettingsError(str(error)) from None
        return store

    def password(self, settings: Settings) -> str:
        try:
            return self.load_secret(
                settings.key_store, self._attributes(settings),
                missing="the password file is missing; enter it again",
                empty="no password stored; enter it in the plugin settings",
            )
        except SettingsError:
            raise
        except _secrets.SecretsError as error:
            raise SettingsError(str(error)) from None

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
        self.clear_keyring(self._attributes(settings))

    @staticmethod
    def _attributes(settings: Settings) -> dict[str, str]:
        return {"server": settings.url, "user": settings.username}
