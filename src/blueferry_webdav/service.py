"""The plugin process: share target, card and notifications for WebDAV."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import secrets
import stat
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.config_flow import ConfigTestResult
from blueferry.plugin_api.manifest import PluginManifest
from blueferry_plugin_kit import xmlsafe
from blueferry_plugin_kit.auth.nextcloud import MESSAGES as LOGIN_MESSAGES
from blueferry_plugin_kit.auth.nextcloud import Credentials, NextcloudLogin
from blueferry_plugin_kit.clipboard import copy_to_clipboard
from blueferry_plugin_kit.configtest import failed, passed, secret_or_stored
from blueferry_plugin_kit.dav.webdav import (
    DavError,
    Entry,
    WebDavClient,
    folder_segments,
    normalize_base,
    safe_name,
)

from blueferry_webdav import __version__
from blueferry_webdav.cache import DownloadCache, blocked
from blueferry_webdav.i18n import german, t
from blueferry_webdav.settings import (
    DEFAULT_FOLDER,
    DEFAULT_MAX_SIZE_MB,
    Settings,
    SettingsError,
    SettingsStore,
)
from blueferry_webdav.surfaces import (
    NOTIFY_ITEM,
    Action,
    CardItem,
    ShareTarget,
    SurfacesService,
    action_result,
)

log = logging.getLogger(__name__)

TARGET_ID = "webdav"
USER_AGENT = f"blueferry-webdav/{__version__}"
# Shown by Nextcloud on "Connect to your account" and in the devices list.
USER_AGENT_LOGIN = "BlueFerry WebDAV"
RECENT_COUNT = 5
LIST_TTL_SEC = 60.0
PROGRESS_INTERVAL_SEC = 1.0
MAX_SHOWN_JOBS = 2
MAX_NOTIFY_ACTIONS = 32
_CONFIG_TEXT = {
    "unauthorized": ("password", "the server rejected user name or password"),
    "forbidden": ("password", "this account may not use the WebDAV address"),
    "not-found": ("url", "no WebDAV folder at this address"),
    "redirect": ("url", "the server redirects; enter the final address"),
    "insecure": ("url", "plain http is only allowed for localhost or, when allowed, the LAN"),
    "invalid-url": ("url", "must be an https:// address without user name or query"),
}


def new_client(*args: Any, **kwargs: Any) -> WebDavClient:
    """A :class:`WebDavClient` that sends this plugin's User-Agent."""
    return WebDavClient(*args, user_agent=USER_AGENT, **kwargs)


_PROPFIND_ZERO = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
)


def _status(client: WebDavClient, method: str, url: str, **kwargs: Any) -> int:
    response = client.request(method, url, **kwargs)
    try:
        return response.status
    finally:
        response.close()  # type: ignore[attr-defined]


def _propfind_status(client: WebDavClient, url: str) -> int:
    return _status(client, "PROPFIND", url, headers={
        "Depth": "0", "Content-Type": "application/xml; charset=utf-8",
    }, body=_PROPFIND_ZERO)


_PRINCIPAL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
_PRINCIPAL_PREFIX = "/remote.php/dav/principals/users/"
MAX_LOGINS = 4


def login_messages() -> dict[str, str]:
    """The sign-in's messages in the user's language."""
    return {key: t(f"login_{key}") for key in LOGIN_MESSAGES}


def new_login(**kwargs: Any) -> NextcloudLogin:
    """The Nextcloud sign-in as this plugin runs it (``kwargs`` for tests)."""
    return NextcloudLogin(user_agent=USER_AGENT_LOGIN, messages=login_messages(), **kwargs)


def nextcloud_user_id(client: WebDavClient, dav_root: str) -> str | None:
    """The Nextcloud user id behind the login, from ``current-user-principal``.

    The files live below ``/remote.php/dav/files/<user id>/``; the login
    name (an e-mail address, for example) can differ from the id.
    """
    response = client.request("PROPFIND", dav_root, headers={
        "Depth": "0", "Content-Type": "application/xml; charset=utf-8",
    }, body=_PRINCIPAL_BODY)
    try:
        if response.status != 207:
            return None
        root = xmlsafe.fromstring(response.read(64 * 1024))
    except (DavError, xmlsafe.XmlError, OSError):
        return None
    finally:
        response.close()  # type: ignore[attr-defined]
    for element in root.iter("{DAV:}current-user-principal"):
        href = urllib.parse.unquote((element.findtext("{DAV:}href") or "").strip())
        path = urllib.parse.urlsplit(href).path
        index = path.find(_PRINCIPAL_PREFIX)
        if index == -1:
            continue
        user = path[index + len(_PRINCIPAL_PREFIX):].strip("/")
        if user and "/" not in user and user not in (".", "..") and all(
            ch.isprintable() for ch in user
        ):
            return user
    return None


def _can_write(client: WebDavClient, folder_url: str) -> bool:
    """Create and remove an empty probe folder below ``folder_url``."""
    probe = f"{folder_url}.blueferry-write-test-{secrets.token_hex(6)}/"
    if _status(client, "MKCOL", probe) not in (200, 201):
        return False
    _status(client, "DELETE", probe)
    return True


def text_for(error: DavError) -> str:
    key = "err_" + error.token
    text = t(key)
    return error.token if text == key else text


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            text = f"{value:.0f}" if unit == "B" else f"{value:.1f}"
            return f"{text.replace('.', ',') if german() else text} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover


def icon_for(name: str, content_type: str) -> str:
    major = content_type.split("/", 1)[0]
    if content_type == "application/pdf" or name.lower().endswith(".pdf"):
        return "application-pdf"
    return {
        "image": "image-x-generic", "video": "video-x-generic", "audio": "audio-x-generic",
        "text": "text-x-generic",
    }.get(major, "text-x-generic" if not content_type else "application-octet-stream")


@dataclass
class Job:
    id: str
    names: list[str]
    total: int
    sent: int = 0
    state: str = "running"  # running, failed
    message: str = ""
    last_emit: float = 0.0

    def item(self) -> CardItem:
        name = self.names[0] if len(self.names) == 1 else t("files", count=len(self.names))
        if self.state == "failed":
            return CardItem(
                f"job-{self.id}", "dialog-error", t("failed", name=name), self.message,
                [Action("dismiss", t("dismiss"), "window-close")],
            )
        percent = 100 if self.total == 0 else min(100, self.sent * 100 // self.total)
        return CardItem(
            f"job-{self.id}", "document-send", t("uploading", name=name),
            t("progress", percent=percent, sent=human_size(self.sent),
              total=human_size(self.total)),
        )


@dataclass
class _Listing:
    key: tuple[object, ...] = ()
    fetched: float = 0.0
    entries: list[Entry] = field(default_factory=list)
    error: str = ""


def entry_id(entry: Entry) -> str:
    return "f-" + hashlib.sha256(entry.url.encode()).hexdigest()[:24]


class WebDavService(SurfacesService):
    def __init__(
        self,
        manifest: PluginManifest,
        bus: Any = None,
        *,
        settings: SettingsStore | None = None,
        cache: DownloadCache | None = None,
        client_factory: Callable[..., WebDavClient] = new_client,
        clipboard: Callable[[str], bool] = copy_to_clipboard,
        login: NextcloudLogin | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(manifest, bus, **kwargs)
        self._login = login or new_login()
        # The form values typed when a sign-in started, by login id.
        self._login_values: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._settings = settings or SettingsStore()
        self._cache = cache or DownloadCache()
        self._client_factory = client_factory
        self._clipboard = clipboard
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._listing = _Listing()
        self._entries: dict[str, Entry] = {}
        self._notify_actions: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._last_error = ""

    # ---- connection --------------------------------------------------------

    def _load(self) -> Settings | None:
        try:
            return self._settings.load()
        except SettingsError as error:
            raise DavError("config:" + str(error)) from None

    def _client(self, settings: Settings) -> WebDavClient:
        try:
            password = self._settings.password(settings)
        except SettingsError as error:
            raise DavError("config:" + str(error)) from None
        return self._client_factory(
            settings.url, settings.username, password,
            allow_http_lan=settings.allow_http_lan, nextcloud=settings.nextcloud,
        )

    @staticmethod
    def _message(error: DavError) -> str:
        # "config:<text>" and "cached:<text>" already carry a message.
        if ":" in error.token:
            return error.token.split(":", 1)[1]
        return text_for(error)

    # ---- Plugin1 -------------------------------------------------------------

    def status(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            return {"state": "error", "detail": str(error)}
        if settings is None:
            return {"state": "unconfigured", "detail": t("setup_hint")}
        server = urllib.parse.urlsplit(settings.url).hostname or ""
        with self._lock:
            running = sum(job.state == "running" for job in self._jobs.values())
            last_error = self._last_error
        if running:
            return {"state": "busy", "server": server, "detail": t("busy", count=running)}
        if last_error:
            return {"state": "error", "server": server, "detail": last_error}
        return {"state": "ok", "server": server}

    def config_values(self) -> dict[str, object]:
        try:
            settings = self._settings.load()
        except SettingsError as error:
            raise ConfigError("", str(error)) from None
        if settings is None:
            return {}
        try:
            stored = bool(self._settings.password(settings))
        except SettingsError:
            stored = False
        return {
            "url": settings.url, "username": settings.username, "password": stored,
            "folder": settings.folder, "public_link": settings.public_link,
            "max_size_mb": settings.max_size_mb, "allow_http_lan": settings.allow_http_lan,
            "web_url": settings.web_url,
        }

    def _validated(self, values: dict[str, object]) -> tuple[Settings, str, Settings | None]:
        """The settings in ``values`` and the password to use (typed or stored).

        Raises ConfigError for a field. Also returns the current settings.
        """
        allow_http_lan = bool(values.get("allow_http_lan"))
        try:
            url = normalize_base(str(values.get("url") or ""), allow_http_lan=allow_http_lan)
        except DavError as error:
            raise ConfigError(*_CONFIG_TEXT.get(error.token, _CONFIG_TEXT["invalid-url"]))
        username = str(values.get("username") or "")
        if not username:
            # Not Required in the manifest: "Sign in with Nextcloud" fills it.
            raise ConfigError("username", "is required")
        if ":" in username or len(username) > 256:
            raise ConfigError("username", "must not contain a colon")
        folder = str(values.get("folder") or DEFAULT_FOLDER)
        try:
            folder = "/".join(folder_segments(folder)) or DEFAULT_FOLDER
        except ValueError as error:
            raise ConfigError("folder", str(error)) from None
        try:
            current = self._settings.load()
        except SettingsError:
            current = None

        def stored() -> str:
            # The stored password belongs to the stored server and user only.
            if current is None or (current.url, current.username) != (url, username):
                return ""
            return self._settings.password(current)

        password = secret_or_stored(values, "password", stored)
        settings = Settings(
            url=url, username=username, folder=folder,
            public_link=bool(values.get("public_link")),
            max_size_mb=int(values.get("max_size_mb") or DEFAULT_MAX_SIZE_MB),
            allow_http_lan=allow_http_lan, web_url=str(values.get("web_url") or ""),
        )
        return settings, password, current

    def _logged_in(self, settings: Settings, password: str) -> tuple[WebDavClient, bool]:
        """A client that has logged in, and whether the server is Nextcloud."""
        client = self._client_factory(
            settings.url, settings.username, password, allow_http_lan=settings.allow_http_lan,
        )
        try:
            client.check()
        except DavError as error:
            field_name, reason = _CONFIG_TEXT.get(
                error.token, ("url", text_for(error)),
            )
            raise ConfigError(field_name, reason) from None
        nextcloud = client.detect_nextcloud()
        if settings.public_link and not nextcloud:
            raise ConfigError(
                "public_link", "needs a Nextcloud address (…/remote.php/dav/files/USER/)",
            )
        return client, nextcloud

    def test_config(self, values: dict[str, object]) -> ConfigTestResult:
        """Worker thread. Log in and probe the folder; store nothing."""
        settings, password, _current = self._validated(values)
        client, _nextcloud = self._logged_in(settings, password)
        segments = folder_segments(settings.folder)
        try:
            exists = _propfind_status(client, client.folder_url(segments)) == 207
            probe_in = client.folder_url(segments) if exists else client.base
            writable = _can_write(client, probe_in)
        except DavError as error:
            raise ConfigError("url", text_for(error)) from None
        log.info("settings tested (writable: %s)", writable)
        user, folder = settings.username, settings.folder
        if not writable:
            return failed(t("test_read_only", user=user, folder=folder),
                          folder=t("test_folder_read_only"))
        return passed(t("test_writable" if exists else "test_will_create",
                        user=user, folder=folder))

    def apply_config(self, values: dict[str, object]) -> None:
        """Worker thread. Log in with the new settings, then store them."""
        settings, password, current = self._validated(values)
        _client, nextcloud = self._logged_in(settings, password)
        url, username = settings.url, settings.username
        settings = dataclasses.replace(settings, nextcloud=nextcloud)
        prefer_keyring = current is None or current.key_store != "file"
        try:
            self._settings.save(settings, password, prefer_keyring=prefer_keyring)
        except (SettingsError, OSError) as error:
            raise ConfigError("", f"could not store the settings: {error}") from None
        if current is not None and current.key_store == "keyring" and (
            (current.url, current.username) != (url, username)
        ):
            self._settings.forget_keyring(current)
        with self._lock:
            self._listing = _Listing()
            self._entries.clear()
            self._last_error = ""
        log.info("settings saved (server type: %s)", "nextcloud" if nextcloud else "webdav")
        self.emit_card_changed()

    # ---- Nextcloud sign-in (ConfigLogin=nextcloud) ---------------------------

    def config_login(self, provider: str, values: dict[str, object]) -> object:
        """Worker thread. Start Login Flow v2 at the typed server."""
        step = self._login.login_step(str(values.get("url") or ""))
        if step.login_id:
            with self._lock:
                self._login_values[step.login_id] = dict(values)
                while len(self._login_values) > MAX_LOGINS:
                    self._login_values.popitem(last=False)
        return step

    def config_login_status(self, login_id: str) -> object:
        with self._lock:
            values = dict(self._login_values.get(login_id, {}))
        step = self._login.status_step(
            login_id, lambda credentials: self._store_login(credentials, values),
        )
        if step.final:
            with self._lock:
                self._login_values.pop(login_id, None)
        return step

    def config_login_cancel(self, login_id: str) -> None:
        self._login.cancel(login_id)
        with self._lock:
            self._login_values.pop(login_id, None)

    def _store_login(self, credentials: Credentials, values: dict[str, object]) -> str:
        """Check the new app password against WebDAV, then store it."""
        try:
            current = self._settings.load()
        except SettingsError:
            current = None
        username, password = credentials.login_name, credentials.app_password
        url = credentials.webdav_url
        client = self._client_factory(url, username, password)
        try:
            try:
                client.check()
            except DavError as error:
                if error.token != "not-found":
                    raise
                # The login name is not the user id (e.g. an e-mail login).
                lookup = self._client_factory(credentials.dav_url + "/", username, password)
                user_id = nextcloud_user_id(lookup, lookup.base)
                if user_id is None:
                    raise
                url = f"{credentials.server}/remote.php/dav/files/" + \
                    urllib.parse.quote(user_id, safe="@") + "/"
                client = self._client_factory(url, username, password)
                client.check()
        except DavError as error:
            field_name, reason = _CONFIG_TEXT.get(error.token, ("url", text_for(error)))
            raise ConfigError(field_name, reason) from None
        def option(key: str, default: object) -> object:
            if key in values:
                return values[key]
            return getattr(current, key) if current is not None else default

        try:
            folder = "/".join(folder_segments(str(option("folder", DEFAULT_FOLDER)))) \
                or DEFAULT_FOLDER
        except ValueError:
            folder = DEFAULT_FOLDER
        settings = Settings(
            url=client.base, username=username, folder=folder,
            public_link=bool(option("public_link", False)),
            max_size_mb=int(option("max_size_mb", DEFAULT_MAX_SIZE_MB) or DEFAULT_MAX_SIZE_MB),
            allow_http_lan=bool(option("allow_http_lan", False)),
            web_url=str(option("web_url", "") or ""), nextcloud=True,
        )
        prefer_keyring = current is None or current.key_store != "file"
        try:
            self._settings.save(settings, password, prefer_keyring=prefer_keyring)
        except (SettingsError, OSError):
            raise ConfigError("", t("login_store-failed")) from None
        if current is not None and current.key_store == "keyring" and (
            (current.url, current.username) != (settings.url, settings.username)
        ):
            self._settings.forget_keyring(current)
        with self._lock:
            self._listing = _Listing()
            self._entries.clear()
            self._last_error = ""
        log.info("signed in with nextcloud; settings saved")
        self.emit_card_changed()
        return t("connected", user=username)

    # ---- share ---------------------------------------------------------------

    def share_targets(self) -> list[ShareTarget]:
        return [ShareTarget(TARGET_ID, t("target_label"), "folder-cloud")]

    def send_files(self, target_id: str, paths: list[str]) -> str:
        def answer(ok: bool, message: str | None, job: str | None = None) -> str:
            return json.dumps({"ok": ok, "message": message, "job": job})

        if target_id != TARGET_ID:
            return answer(False, t("unknown_target"))
        if not paths:
            return answer(False, t("no_files"))
        try:
            settings = self._load()
        except DavError as error:
            return answer(False, self._message(error))
        if settings is None:
            return answer(False, t("not_set_up_send", hint=t("setup_hint")))
        files: list[tuple[str, str, int]] = []
        for path in paths:
            name = safe_name(path)
            if not os.path.isabs(path) or "\x00" in path:
                return answer(False, t("not_absolute", name=name))
            try:
                info = os.stat(path)
            except OSError:
                return answer(False, t("not_readable", name=name))
            if not stat.S_ISREG(info.st_mode):
                return answer(False, t("not_regular", name=name))
            if info.st_size > settings.max_bytes:
                return answer(False, t("too_big", name=name, limit=settings.max_size_mb))
            if not os.access(path, os.R_OK):
                return answer(False, t("not_readable", name=name))
            files.append((path, name, info.st_size))
        job = Job(id=secrets.token_hex(8), names=[name for _p, name, _s in files],
                  total=sum(size for _p, _n, size in files))
        with self._lock:
            self._jobs[job.id] = job
        log.info("upload started: %d file(s)", len(files))
        self.emit_card_changed()
        self.start_job(lambda: self._upload(job, files, settings))
        return answer(True, t("upload_started"), job.id)

    def _progress(self, job: Job, sent: int) -> None:
        now = self._clock()
        with self._lock:
            job.sent += sent
            due = now - job.last_emit >= PROGRESS_INTERVAL_SEC
            if due:
                job.last_emit = now
        if due:
            self.emit_card_changed()

    def _upload(self, job: Job, files: list[tuple[str, str, int]], settings: Settings) -> None:
        try:
            client = self._client(settings)
            segments = folder_segments(settings.folder)
            folder_url = client.ensure_folder(segments)
            stored: list[str] = []
            for path, name, _size in files:
                with open(path, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > settings.max_bytes:
                        raise DavError("too-large")
                    remote = client.free_name(folder_url, name)
                    client.upload(
                        stream, info.st_size, folder_url, remote,
                        progress=lambda sent: self._progress(job, sent),
                    )
                stored.append(remote)
        except (DavError, OSError, ValueError) as error:
            message = self._message(error) if isinstance(error, DavError) else (
                t("file_unreadable") if isinstance(error, OSError) else t("bad_folder")
            )
            log.info("upload failed: %s", getattr(error, "token", type(error).__name__))
            with self._lock:
                job.state, job.message = "failed", message
                self._last_error = message
            self.emit_card_changed()
            self.emit_notify(t("upload_failed"), message, "dialog-error")
            return
        link = None
        link_failed = False
        if settings.public_link and client.nextcloud is not None and len(stored) == 1:
            try:
                link = client.share_link(segments, stored[0])
            except DavError as error:
                link_failed = True
                log.info("public link failed: %s", error.token)
        with self._lock:
            self._jobs.pop(job.id, None)
            self._listing = _Listing()
            self._last_error = ""
        log.info("upload finished: %d file(s)", len(stored))
        self.emit_card_changed()
        what = stored[0] if len(stored) == 1 else t("files", count=len(stored))
        body = f"{what} → {'/'.join(segments)}"
        if link_failed:
            body += t("link_failed")
        if link:
            action_id = self._remember_action("link", link)
            self.emit_notify(t("uploaded"), body, "folder-cloud", t("copy_link"), action_id)
        else:
            action_id = self._remember_action("folder", self._folder_web_url(client, settings))
            self.emit_notify(t("uploaded"), body, "folder-cloud", t("open_folder"), action_id)

    def _folder_web_url(self, client: WebDavClient, settings: Settings) -> str:
        segments = folder_segments(settings.folder)
        return settings.web_url or client.web_folder_url(segments) or client.folder_url(segments)

    def _remember_action(self, kind: str, value: str) -> str:
        action_id = f"{kind}-{secrets.token_hex(6)}"
        with self._lock:
            self._notify_actions[action_id] = (kind, value)
            while len(self._notify_actions) > MAX_NOTIFY_ACTIONS:
                self._notify_actions.popitem(last=False)
        return action_id

    # ---- card ----------------------------------------------------------------

    def _recent(self, settings: Settings, *, force: bool = False) -> list[Entry]:
        key = (settings.url, settings.username, settings.folder)
        now = self._clock()
        with self._lock:
            listing = self._listing
            fresh = listing.key == key and now - listing.fetched < LIST_TTL_SEC
            if fresh and not force:
                if listing.error:
                    raise DavError("cached:" + listing.error)
                return listing.entries
        try:
            entries = self._client(settings).list_folder(folder_segments(settings.folder))
        except DavError as error:
            message = self._message(error)
            log.info("listing failed: %s", error.token.split(":", 1)[0])
            with self._lock:
                self._listing = _Listing(key, now, [], message)
            raise DavError("cached:" + message) from None
        entries.sort(key=lambda entry: (entry.modified, entry.name), reverse=True)
        entries = entries[:RECENT_COUNT]
        with self._lock:
            self._listing = _Listing(key, now, entries)
            self._entries = {entry_id(entry): entry for entry in entries}
        return entries

    def card_items(self) -> list[CardItem]:
        with self._lock:
            jobs = [job.item() for job in list(self._jobs.values())[-MAX_SHOWN_JOBS:]]
        try:
            settings = self._load()
        except DavError as error:
            return [*jobs, CardItem("setup", "dialog-error", t("target_label"),
                                    self._message(error))]
        if settings is None:
            return [*jobs, CardItem(
                "setup", "folder-remote", t("not_set_up"), t("setup_hint"),
            )]
        host = urllib.parse.urlsplit(settings.url).hostname or ""
        header_actions = [
            Action("refresh", t("refresh"), "view-refresh"),
            Action("open_folder", t("open_folder"), "folder-open"),
        ]
        try:
            entries = self._recent(settings)
        except DavError as error:
            return [*jobs, CardItem(
                "folder", "dialog-warning", t("recent"),
                self._message(error), header_actions,
            )]
        subtitle = f"{host} · /{settings.folder}"
        if not entries:
            subtitle += t("nothing_yet")
        items = [*jobs, CardItem("folder", "folder-remote", t("recent"), subtitle,
                                 header_actions)]
        for entry in entries:
            when = time.strftime(t("date"), time.localtime(entry.modified)) \
                if entry.modified else ""
            items.append(CardItem(
                entry_id(entry), icon_for(entry.name, entry.content_type), entry.name,
                " · ".join(part for part in (human_size(entry.size), when) if part),
                [Action("open", t("open"), "document-open", "primary")],
            ))
        return items

    # ---- actions -------------------------------------------------------------

    def invoke_action(self, item_id: str, action_id: str, args: dict[str, Any]) -> str:
        if item_id == NOTIFY_ITEM:
            return self._notify_action(action_id)
        if item_id == "folder":
            return self._folder_action(action_id)
        if item_id.startswith("job-") and action_id == "dismiss":
            with self._lock:
                job = self._jobs.get(item_id[4:])
                if job is not None and job.state == "failed":
                    del self._jobs[job.id]
            self.emit_card_changed()
            return action_result(True)
        if item_id.startswith("f-") and action_id == "open":
            return self._open(item_id)
        return action_result(False, t("unknown_action"))

    def _notify_action(self, action_id: str) -> str:
        with self._lock:
            known = self._notify_actions.get(action_id)
        if known is None:
            return action_result(False, t("expired"))
        kind, value = known
        if kind == "link":
            if self._clipboard(value):
                return action_result(True, t("link_copied"))
            return action_result(True, t("no_clipboard"), value)
        return action_result(True, None, value)

    def _folder_action(self, action_id: str) -> str:
        try:
            settings = self._load()
        except DavError as error:
            return action_result(False, self._message(error))
        if settings is None:
            return action_result(False, t("setup_hint"))
        if action_id == "refresh":
            with self._lock:
                self._listing = _Listing()
            self.emit_card_changed()
            return action_result(True, t("refreshed"))
        if action_id == "open_folder":
            try:
                client = self._client(settings)
            except DavError as error:
                return action_result(False, self._message(error))
            return action_result(True, None, self._folder_web_url(client, settings))
        return action_result(False, t("unknown_action"))

    def _open(self, item_id: str) -> str:
        try:
            settings = self._load()
            if settings is None:
                return action_result(False, t("setup_hint"))
            with self._lock:
                entry = self._entries.get(item_id)
            if entry is None:
                # Not listed by this process (it may have idled out since).
                self._recent(settings, force=True)
                with self._lock:
                    entry = self._entries.get(item_id)
            if entry is None:
                return action_result(False, t("gone"))
            name = safe_name(entry.name)
            if blocked(name):
                return action_result(False, t("blocked"))
            if entry.size > settings.max_bytes:
                return action_result(False, t("larger_than", limit=settings.max_size_mb))
            path = self._cache.cached(item_id, name, entry.size)
            if path is None:
                client = self._client(settings)
                path = self._cache.store(
                    item_id, name,
                    lambda stream: client.download(entry.url, stream, settings.max_bytes),
                )
        except DavError as error:
            return action_result(False, self._message(error))
        except OSError:
            return action_result(False, t("cache_error"))
        return action_result(True, None, path.as_uri())
