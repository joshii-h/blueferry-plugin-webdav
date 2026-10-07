"""The plugin against a real WebDAV server and a fake Nextcloud; no network."""
from __future__ import annotations

import io
import json
import logging
import os
import urllib.parse
from pathlib import Path

import pytest
from blueferry.plugin_api.testing import inline_service
from conftest import PASSWORD, USER, FakeSecret, serve
from fakehost import FakeHost

from blueferry_webdav import CAPABILITIES, PLUGIN_ID, load_manifest, manifest_text
from blueferry_webdav import __main__ as cli
from blueferry_webdav.cache import DownloadCache
from blueferry_webdav.service import WebDavService
from blueferry_webdav.settings import SettingsStore
from blueferry_webdav.surfaces import CardItem, plain
from blueferry_webdav.webdav import (
    DavError,
    WebDavClient,
    folder_segments,
    normalize_base,
    parse_multistatus,
    safe_name,
)

# ---- helpers ------------------------------------------------------------------


class Clock:
    """Advances two seconds per reading, so every progress step is reported."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 2
        return self.now


@pytest.fixture
def plugin(tmp_path):
    cache_root = Path(os.environ["XDG_CACHE_HOME"]) / "blueferry"
    copied: list[str] = []

    def make(*, clipboard=None, refetch=False):
        def copy(text: str) -> bool:
            copied.append(text)
            return clipboard is not False

        service = inline_service(
            WebDavService, load_manifest(), None,
            settings=SettingsStore(tmp_path / "config", secret=FakeSecret()),
            cache=DownloadCache(cache_root / "webdav"),
            clipboard=copy, clock=Clock(),
        )
        host = FakeHost(service, cache_root=cache_root, refetch=refetch)
        host.copied = copied
        return host

    return make


def configure(host, url, **extra):
    values = {"url": url, "username": USER, "password": PASSWORD, **extra}
    return host.set_config(values)


def write(tmp_path, name, data):
    path = tmp_path / "local" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return str(path)


# ---- manifest and contract ----------------------------------------------------


def test_manifest_declares_the_surfaces_and_settings() -> None:
    manifest = load_manifest()
    assert manifest.id == PLUGIN_ID
    assert manifest.capabilities == CAPABILITIES
    assert "ApiVersion=1.2" in manifest_text()
    keys = [field.key for field in manifest.config]
    assert keys == ["url", "username", "password", "folder", "public_link", "max_size_mb",
                    "allow_http_lan", "web_url"]
    defaults = {field.key: field.default for field in manifest.config}
    assert defaults["folder"] == "BlueFerry" and defaults["public_link"] is False


def test_all_surfaces_live_on_plugin1() -> None:
    from blueferry.plugin_api import PLUGIN_INTERFACE

    table = WebDavService._dbus_class_table[f"{WebDavService.__module__}.WebDavService"]
    assert set(table) - {"org.freedesktop.DBus.Introspectable"} == {PLUGIN_INTERFACE}
    members = set(table[PLUGIN_INTERFACE])
    assert {"GetCardItems", "CardChanged", "InvokeAction", "ShareTargets", "SendFiles",
            "Notify", "GetInfo", "Status", "GetConfig", "SetConfig"} <= members


def test_info_reports_contract_1_2(plugin) -> None:
    host = plugin()
    info = host.info()
    assert info["api_version"] == 1 and info["api_minor"] >= 2
    assert set(info["capabilities"]) == {"card", "share", "notify"}
    assert host.share_targets() == [
        {"id": "webdav", "label": "Ablage (WebDAV)", "icon": "folder-cloud"},
    ]


def test_card_text_is_clipped_to_the_spec() -> None:
    item = CardItem("x", "bad icon!", "t" * 200, "s\n" * 200).as_json()
    assert len(item["title"]) == 80 and len(item["subtitle"]) == 160
    assert item["icon"] == "application-x-addon"
    assert plain("a\x1b[31mb‮c", 80) == "a [31mb c"


def test_unconfigured(plugin, tmp_path) -> None:
    host = plugin()
    assert host.status()["state"] == "unconfigured"
    items = host.card_items()
    assert [item["id"] for item in items] == ["setup"]
    result = host.send_files("webdav", [write(tmp_path, "a.txt", b"a")])
    assert result["ok"] is False and "nicht eingerichtet" in result["message"]


def test_invoke_action_rejects_bad_input(plugin) -> None:
    host = plugin()
    assert host.invoke("../x", "open")["ok"] is False
    assert host.invoke("folder", "refresh", "[1]")["ok"] is False
    assert host.invoke("notify", "link-unknown")["message"].startswith("Diese Benachrichtigung")


# ---- URLs and names -----------------------------------------------------------


@pytest.mark.parametrize("raw,lan,expected", [
    ("https://cloud.example.org/remote.php/dav/files/me", False,
     "https://cloud.example.org/remote.php/dav/files/me/"),
    ("https://dav.example.org", False, "https://dav.example.org/"),
    ("http://localhost:8080/dav/", False, "http://localhost:8080/dav/"),
    ("http://192.168.1.5/dav", True, "http://192.168.1.5/dav/"),
    ("http://nas.local/dav", True, "http://nas.local/dav/"),
    ("http://nas/dav", True, "http://nas/dav/"),
])
def test_urls_are_normalized(raw, lan, expected) -> None:
    assert normalize_base(raw, allow_http_lan=lan) == expected


@pytest.mark.parametrize("raw,lan,token", [
    ("http://192.168.1.5/dav", False, "insecure"),
    ("http://dav.example.org/", True, "insecure"),
    ("http://8.8.8.8/", True, "insecure"),
    ("https://user:pw@dav.example.org/", False, "invalid-url"),
    ("https://dav.example.org/?x=1", False, "invalid-url"),
    ("https://dav.example.org/a/../b", False, "invalid-url"),
    ("https://dav.example.org/a/%2e%2e/b", False, "invalid-url"),
    ("ftp://dav.example.org/", False, "invalid-url"),
    ("", False, "invalid-url"),
])
def test_bad_urls_are_refused(raw, lan, token) -> None:
    with pytest.raises(DavError) as caught:
        normalize_base(raw, allow_http_lan=lan)
    assert caught.value.token == token


def test_lan_http_is_checked_against_resolved_addresses() -> None:
    client = WebDavClient("http://nas.local/dav/", USER, PASSWORD, allow_http_lan=True,
                          resolve=lambda host: ["93.184.216.34"])
    with pytest.raises(DavError) as caught:
        client.check()
    assert caught.value.token == "insecure"


def test_lan_http_connects_to_the_checked_address_only(dav_server) -> None:
    port = urllib.parse.urlsplit(dav_server.url).port
    answers = iter([["127.0.0.1"], ["93.184.216.34"]])
    lookups: list[str] = []

    def resolve(host: str) -> list[str]:
        lookups.append(host)
        return next(answers)

    client = WebDavClient(f"http://nas.local:{port}/dav/", USER, PASSWORD, allow_http_lan=True,
                          resolve=resolve)
    client.check()
    client.list_folder(())
    client.ensure_folder(("pinned",))
    # One lookup for the whole operation: a second answer (now public)
    # is never asked for, and the requests went to the checked address.
    assert lookups == ["nas.local"]


def test_requests_never_leave_the_configured_server() -> None:
    client = WebDavClient("https://dav.example.org/", USER, PASSWORD)
    with pytest.raises(DavError) as caught:
        client.download("https://evil.example.org/x", io.BytesIO(), 10)
    assert caught.value.token == "invalid-url"
    assert PASSWORD not in repr(client)


@pytest.mark.parametrize("raw,expected", [
    ("/home/me/report.pdf", "report.pdf"),
    ("/tmp/../../etc/passwd", "passwd"),
    ("/tmp/a\\..\\b.txt", "a_.._b.txt"),
    ("/tmp/.hidden", "hidden"),
    ("/tmp/...", "file"),
    ("/tmp/bad\x1bname\n.txt", "bad_name_.txt"),
    ("/tmp/Café.txt", "Café.txt"),
])
def test_file_names_are_made_safe(raw, expected) -> None:
    assert safe_name(raw) == expected


def test_long_names_keep_their_extension() -> None:
    name = safe_name("/tmp/" + "ä" * 300 + ".jpeg")
    assert name.endswith(".jpeg") and len(name.encode()) <= 200


def test_folder_segments() -> None:
    assert folder_segments(" Phone//Uploads/./x ") == ("Phone", "Uploads", "x")
    assert folder_segments("") == ()
    for bad in ("a/../b", "..", "a/\x00b"):
        with pytest.raises(ValueError):
            folder_segments(bad)


def test_listing_ignores_foreign_and_nested_entries() -> None:
    folder = "https://dav.example.org/files/BlueFerry/"
    xml = b"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
    <d:response><d:href>/files/BlueFerry/</d:href><d:propstat><d:prop>
      <d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    <d:response><d:href>/files/BlueFerry/a%20b.txt</d:href><d:propstat><d:prop>
      <d:resourcetype/><d:getcontentlength>12</d:getcontentlength>
      <d:getlastmodified>Mon, 06 Oct 2026 18:00:00 GMT</d:getlastmodified></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    <d:response><d:href>/files/BlueFerry/sub/x.txt</d:href></d:response>
    <d:response><d:href>/files/other.txt</d:href></d:response>
    <d:response><d:href>https://evil.example.org/files/BlueFerry/e.txt</d:href></d:response>
    <d:response><d:href>/files/BlueFerry/%2e%2e</d:href></d:response>
    </d:multistatus>"""
    entries = parse_multistatus(xml, folder)
    assert [(e.name, e.size) for e in entries] == [("a b.txt", 12)]
    assert entries[0].url == folder + "a%20b.txt" and entries[0].modified > 0
    with pytest.raises(DavError):
        parse_multistatus(b'<!DOCTYPE x [<!ENTITY a "b">]><x/>', folder)


# ---- generic WebDAV (wsgidav) ---------------------------------------------------


def test_settings_are_checked_against_the_server(plugin, dav_server) -> None:
    host = plugin()
    wrong = host.set_config({"url": dav_server.url, "username": USER, "password": "nope"})
    assert wrong == {"ok": False, "errors": {"password": wrong["errors"]["password"]}}
    no_nextcloud = configure(host, dav_server.url, public_link=True)
    assert "public_link" in no_nextcloud["errors"]
    traversal = configure(host, dav_server.url, folder="../etc")
    assert "folder" in traversal["errors"]
    assert configure(host, dav_server.url) == {"ok": True}
    values = host.get_config()["values"]
    assert values["password"] == "********" and values["folder"] == "BlueFerry"
    assert values["max_size_mb"] == 2048
    assert host.status() == {"state": "ok", "server": "127.0.0.1"}


def test_upload_lists_and_opens(plugin, dav_server, tmp_path) -> None:
    host = plugin()
    assert configure(host, dav_server.url, folder="Phone/Uploads") == {"ok": True}
    first = write(tmp_path, "we ird #?%.txt", b"hello")
    result = host.send_files("webdav", [first])
    assert result["ok"] is True and result["job"]
    stored = dav_server.root / "Phone" / "Uploads" / "we ird #?%.txt"
    assert stored.read_bytes() == b"hello"
    # The same name again does not overwrite.
    host.send_files("webdav", [write(tmp_path, "we ird #?%.txt", b"second")])
    assert (stored.parent / "we ird #?% (2).txt").read_bytes() == b"second"
    assert stored.read_bytes() == b"hello"

    title, body, _icon, label, _action = host.notifications[-1]
    assert (title, label) == ("Hochgeladen", "Ordner öffnen")
    assert "Phone/Uploads" in body
    opened = host.click_notification()
    assert opened["open_uri"] == dav_server.url + "Phone/Uploads/"

    items = host.card_items()
    assert items[0]["id"] == "folder"
    assert [a["id"] for a in items[0]["actions"]] == ["refresh", "open_folder"]
    names = [item["title"] for item in items[1:]]
    assert set(names) == {"we ird #?%.txt", "we ird #?% (2).txt"}
    entry = next(item for item in items if item["title"] == "we ird #?% (2).txt")
    result = host.invoke(entry["id"], "open")
    assert result["ok"] is True
    local = Path(urllib.parse.unquote(result["open_uri"][len("file://"):]))
    assert local.read_bytes() == b"second"
    assert oct(local.stat().st_mode & 0o777) == "0o600"


def test_texts_follow_the_locale(plugin, dav_server, tmp_path, monkeypatch) -> None:
    from blueferry_webdav import i18n

    monkeypatch.setenv("LANG", "en_US.UTF-8")
    host = plugin()
    assert host.share_targets()[0]["label"] == "Storage (WebDAV)"
    assert host.card_items()[0]["title"] == "Storage (WebDAV) not set up"
    assert configure(host, dav_server.url) == {"ok": True}
    host.send_files("webdav", [write(tmp_path, "a.txt", b"x" * 1500)])
    title, _body, _icon, label, _action = host.notifications[-1]
    assert (title, label) == ("Uploaded", "Open folder")
    folder = host.card_items()[0]
    assert folder["title"] == "Recently uploaded"
    assert [a["label"] for a in folder["actions"]] == ["Refresh", "Open folder"]
    assert "1.5 KB" in host.card_items()[1]["subtitle"]
    # Both tables have the same keys.
    assert i18n._DE.keys() == i18n._EN.keys()


def test_card_shows_the_newest_five(plugin, dav_server, tmp_path) -> None:
    host = plugin()
    configure(host, dav_server.url)
    folder = dav_server.root / "BlueFerry"
    folder.mkdir()
    for number in range(7):
        path = folder / f"f{number}.txt"
        path.write_text(str(number))
        os.utime(path, (1_700_000_000 + number * 100,) * 2)
    (folder / "sub").mkdir()
    items = host.card_items()
    assert [item["title"] for item in items[1:]] == ["f6.txt", "f5.txt", "f4.txt",
                                                       "f3.txt", "f2.txt"]
    (folder / "f9.txt").write_text("new")
    assert len(host.card_items()) == 6  # cached for a minute
    changes = host.card_changes
    assert host.invoke("folder", "refresh")["ok"] is True
    assert host.card_changes == changes + 1
    assert host.card_items()[1]["title"] == "f9.txt"


def test_open_refuses_launchers(plugin, dav_server) -> None:
    host = plugin()
    configure(host, dav_server.url)
    (dav_server.root / "BlueFerry").mkdir()
    (dav_server.root / "BlueFerry" / "evil.desktop").write_text("[Desktop Entry]\nExec=x")
    item = host.card_items()[1]
    result = host.invoke(item["id"], "open")
    assert result["ok"] is False and result["open_uri"] is None


def test_size_limit_and_bad_paths(plugin, dav_server, tmp_path) -> None:
    host = plugin()
    configure(host, dav_server.url, max_size_mb=1)
    big = write(tmp_path, "big.bin", b"x" * (1024 * 1024 + 1))
    assert "größer als 1 MB" in host.send_files("webdav", [big])["message"]
    assert host.send_files("webdav", [str(tmp_path)])["ok"] is False
    assert host.send_files("webdav", ["relative.txt"])["ok"] is False
    assert host.send_files("other", [big])["ok"] is False
    assert not (dav_server.root / "BlueFerry").exists()


def test_progress_is_reported_on_the_card(plugin, dav_server, tmp_path) -> None:
    host = plugin(refetch=True)
    configure(host, dav_server.url)
    data = os.urandom(1024 * 1024)
    host.send_files("webdav", [write(tmp_path, "movie.mov", data)])
    progress = [item["subtitle"] for snapshot in host.snapshots for item in snapshot
                if item["title"] == "Lädt hoch: movie.mov"]
    assert progress[0].startswith("0 %")
    assert any(text.startswith("50 %") for text in progress)
    assert progress[-1].startswith("100 %")
    assert all(item["title"] != "Lädt hoch: movie.mov" for item in host.snapshots[-1])
    assert (dav_server.root / "BlueFerry" / "movie.mov").read_bytes() == data


def test_failed_upload_is_shown_and_dismissed(plugin, dav_server, tmp_path) -> None:
    host = plugin()
    configure(host, dav_server.url)
    dav_server.server.stop()
    host.send_files("webdav", [write(tmp_path, "a.txt", b"a")])
    assert host.notifications[-1][:2] == ("Hochladen fehlgeschlagen", "Server nicht erreichbar")
    job = host.card_items()[0]
    assert job["title"] == "Fehlgeschlagen: a.txt" and job["actions"][0]["id"] == "dismiss"
    assert host.status()["state"] == "error"
    host.invoke(job["id"], "dismiss")
    assert all(not item["id"].startswith("job-") for item in host.card_items())


def test_password_and_content_never_reach_logs(plugin, dav_server, tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    host = plugin()
    configure(host, dav_server.url)
    secret_content = b"top-secret-content-123"
    host.send_files("webdav", [write(tmp_path, "c.txt", secret_content)])
    items = host.card_items()
    host.invoke(items[1]["id"], "open")
    text = caplog.text + json.dumps(items) + json.dumps(host.get_config())
    assert PASSWORD not in text
    assert secret_content.decode() not in text


def test_redirects_are_refused(tmp_path) -> None:
    def app(_environ, start_response):
        start_response("302 Found", [("Location", "https://evil.example.org/"),
                                     ("Content-Length", "0")])
        return [b""]

    server = serve(app)
    try:
        client = WebDavClient(f"http://127.0.0.1:{server.server_port}/", USER, PASSWORD)
        with pytest.raises(DavError) as caught:
            client.check()
        assert caught.value.token == "redirect"
    finally:
        server.stop()


# ---- Nextcloud ----------------------------------------------------------------


def test_nextcloud_chunked_upload_and_public_link(plugin, nextcloud, tmp_path) -> None:
    host = plugin()
    assert configure(host, nextcloud.url, public_link=True) == {"ok": True}
    data = os.urandom(64 * 1024 * 3 + 123)
    host.send_files("webdav", [write(tmp_path, "clip.mp4", data)])
    assert nextcloud.files["/BlueFerry/clip.mp4"] == data
    chunk_puts = [path for method, path in nextcloud.log
                  if method == "PUT" and "/uploads/" in path]
    assert len(chunk_puts) == 4 and chunk_puts[0].endswith("/00001")
    assert ("MOVE" in {method for method, _path in nextcloud.log})
    assert nextcloud.uploads == {}
    assert nextcloud.shares[0]["path"] == "/BlueFerry/clip.mp4"
    assert nextcloud.shares[0]["permissions"] == "1"

    title, _body, _icon, label, _action = host.notifications[-1]
    assert (title, label) == ("Hochgeladen", "Link kopieren")
    result = host.click_notification()
    assert result == {"ok": True, "message": "Link kopiert", "open_uri": None}
    assert host.copied == ["https://cloud.example.org/s/tok1"]


def test_nextcloud_small_files_use_one_put(plugin, nextcloud, tmp_path) -> None:
    host = plugin(clipboard=False)
    configure(host, nextcloud.url, public_link=True)
    host.send_files("webdav", [write(tmp_path, "note.txt", b"small")])
    assert not any("/uploads/" in path for _method, path in nextcloud.log)
    assert nextcloud.files["/BlueFerry/note.txt"] == b"small"
    result = host.click_notification()
    assert result["open_uri"] == "https://cloud.example.org/s/tok1"  # no clipboard


def test_nextcloud_folder_link_and_listing(plugin, nextcloud, tmp_path) -> None:
    host = plugin()
    configure(host, nextcloud.url, folder="Phone Uploads")
    paths = [write(tmp_path, f"{n}.txt", str(n).encode()) for n in range(2)]
    host.send_files("webdav", paths)
    title, body, _icon, label, _action = host.notifications[-1]
    assert (title, label) == ("Hochgeladen", "Ordner öffnen") and body.startswith("2 Dateien")
    assert host.click_notification()["open_uri"] == (
        nextcloud.base + "/nc/index.php/apps/files/?dir=/Phone%20Uploads"
    )
    items = host.card_items()
    assert [item["title"] for item in items[1:]] == ["1.txt", "0.txt"]
    opened = host.invoke(items[1]["id"], "open")
    assert Path(opened["open_uri"][len("file://"):]).read_bytes() == b"1"


def test_nextcloud_failed_chunk_cleans_up(plugin, nextcloud, tmp_path) -> None:
    host = plugin()
    configure(host, nextcloud.url)
    nextcloud.fail_chunk = 2
    host.send_files("webdav", [write(tmp_path, "big.bin", os.urandom(64 * 1024 * 3))])
    assert host.notifications[-1][1] == "kein Speicherplatz mehr auf dem Server"
    assert nextcloud.uploads == {}
    assert ("DELETE" in {method for method, _path in nextcloud.log})
    assert "/BlueFerry/big.bin" not in nextcloud.files


def test_web_url_overrides_open_folder(plugin, dav_server, tmp_path) -> None:
    host = plugin()
    configure(host, dav_server.url, web_url="https://files.example.org/web/client/files")
    assert host.invoke("folder", "open_folder")["open_uri"] == (
        "https://files.example.org/web/client/files"
    )


# ---- command line -------------------------------------------------------------


def test_cli_status_and_forget(capsys) -> None:
    assert cli.main(["status"]) == 0
    assert "Not configured" in capsys.readouterr().out
    assert cli.main(["forget"]) == 0


# ---- clipboard helper (from blueferry-plugin-shortcuts) -------------------------


class _Runner:
    def __init__(self, help_text="  --sensitive  Hint", returncode=0) -> None:
        self.calls: list = []
        self.help_text = help_text
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls.append((argv, kwargs))
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, self.help_text, "")
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"")


def _which(name: str) -> str:
    return f"/usr/bin/{name}"


def test_clipboard_uses_stdin_sensitive_hint_and_a_clean_environment() -> None:
    import subprocess

    from blueferry_webdav.clipboard import copy_to_clipboard

    runner = _Runner()
    environ = {"WAYLAND_DISPLAY": "wayland-0", "PATH": "/usr/bin", "SECRET_ENV": "x",
               "LC_ALL": "C"}
    assert copy_to_clipboard("https://cloud/s/abc", environ=environ, which=_which, run=runner)
    argv, kwargs = runner.calls[-1]
    assert argv == ["/usr/bin/wl-copy", "--type", "text/plain;charset=utf-8", "--sensitive"]
    assert kwargs["input"] == b"https://cloud/s/abc" and kwargs["stdout"] == subprocess.DEVNULL
    assert "SECRET_ENV" not in kwargs["env"] and kwargs["env"]["LC_ALL"] == "C"


def test_clipboard_finds_the_wayland_socket_and_falls_back_to_x11(tmp_path) -> None:
    import socket

    from blueferry_webdav.clipboard import copy_to_clipboard, helper_environment

    runtime = tmp_path / "run"
    runtime.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(runtime / "wayland-1"))
        env = helper_environment({"XDG_RUNTIME_DIR": str(runtime)})
        assert env is not None and env["WAYLAND_DISPLAY"] == "wayland-1"
    assert helper_environment({"XDG_RUNTIME_DIR": str(tmp_path / "none")}) is None
    runner = _Runner()
    x11 = {"DISPLAY": ":0", "XAUTHORITY": "/tmp/xa", "SECRET_ENV": "x",
           "XDG_RUNTIME_DIR": str(tmp_path / "none")}
    assert copy_to_clipboard("link", environ=x11, which=_which, run=runner)
    argv, kwargs = runner.calls[-1]
    assert argv[0] == "/usr/bin/xclip" and kwargs["input"] == b"link"
    assert kwargs["env"] == {"DISPLAY": ":0", "XAUTHORITY": "/tmp/xa"}
    assert not copy_to_clipboard("link", environ={}, which=_which, run=runner)
