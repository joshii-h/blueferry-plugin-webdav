"""Sign in with Nextcloud (Login Flow v2) against a fake Nextcloud over https."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from blueferry.plugin_api.testing import inline_service
from blueferry_plugin_kit.testing import FakeNextcloudLogin, FakeSecret, TestCA, WsgiServer
from conftest import PASSWORD, USER
from fake_nextcloud import FakeNextcloud
from fakehost import FakeHost

from blueferry_webdav import load_manifest
from blueferry_webdav.cache import DownloadCache
from blueferry_webdav.service import WebDavService, new_login
from blueferry_webdav.settings import SettingsStore


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> TestCA:
    return TestCA(tmp_path_factory.mktemp("ca"))


@pytest.fixture
def cloud(ca, monkeypatch):
    """Login flow and WebDAV files of one fake Nextcloud below /nc, over https."""
    monkeypatch.setenv("SSL_CERT_FILE", str(ca.store.ca_cert_path))
    files = FakeNextcloud(USER, PASSWORD, min_chunk=64 * 1024)
    login = FakeNextcloudLogin(prefix="/nc", login_name=USER, app_password=PASSWORD,
                               fallback=files)
    with WsgiServer(login, ca=ca) as server:
        files.base = server.base
        login.url = f"{server.base}/nc"
        login.files = files
        yield login


@pytest.fixture
def make(tmp_path, ca):
    secret = FakeSecret()

    def build(*, clock=None):
        kwargs = {"clock": clock} if clock else {}
        flow = new_login(context=ca.client_context(), min_interval=0, **kwargs)
        cache_root = Path(os.environ["XDG_CACHE_HOME"]) / "blueferry"
        service = inline_service(
            WebDavService, load_manifest(), None,
            settings=SettingsStore(tmp_path / "config", secret=secret),
            cache=DownloadCache(cache_root / "webdav"), clipboard=lambda text: True,
            login=flow,
        )
        host = FakeHost(service, cache_root=cache_root)
        host.secret = secret
        return host

    return build


def test_manifest_offers_the_sign_in() -> None:
    manifest = load_manifest()
    assert manifest.config_login == "nextcloud" and manifest.config_test is True


def test_sign_in_stores_the_address_and_app_password(make, cloud, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    host = make()
    typed = {"url": cloud.url + "/remote.php/dav/files/someone/", "username": "",
             "folder": "Phone/Uploads"}
    step = host.sign_in(typed, before_poll=lambda n: n == 1 and cloud.grant())
    assert step == {"state": "done", "message": f"Verbunden als {USER}"}
    assert host.opened[0].startswith(cloud.url + "/login/v2/flow/")
    assert ("POST", "/nc/index.php/login/v2", "BlueFerry WebDAV") in cloud.requests
    values = host.get_config()["values"]
    assert values["url"] == f"{cloud.url}/remote.php/dav/files/{USER}/"
    assert values["username"] == USER and values["password"] == "********"
    assert values["folder"] == "Phone/Uploads"
    assert host.card_changed >= 1
    # The app password went into the keyring, nowhere else.
    stored = SettingsStore(host.service._settings.directory)
    assert PASSWORD not in stored.config_path.read_text()
    assert list(host.secret.items.values()) == [PASSWORD]
    assert not stored.key_path.exists()
    host.assert_never_sent(PASSWORD)
    assert PASSWORD not in caplog.text
    assert host.status()["state"] == "ok"
    # The stored settings work: the card lists the (empty) folder.
    assert host.card_items()[0]["id"] == "folder"


def test_sign_in_finds_the_user_id_behind_an_email_login(make, cloud) -> None:
    cloud.files.aliases.add("alice@example.org")
    cloud.login_name = "alice@example.org"
    cloud.grant_after = 1
    host = make()
    step = host.sign_in({"url": cloud.url})
    assert step == {"state": "done", "message": "Verbunden als alice@example.org"}
    values = host.get_config()["values"]
    assert values["url"] == f"{cloud.url}/remote.php/dav/files/{USER}/"
    assert values["username"] == "alice@example.org"


def test_plain_http_is_refused(make, cloud) -> None:
    host = make()
    step = host.sign_in({"url": cloud.url.replace("https://", "http://")})
    assert step["state"] == "error" and "https" in step["message"]
    assert host.opened == [] and cloud.requests == []


def test_cancel_and_expiry(make, cloud) -> None:
    now = [0.0]
    host = make(clock=lambda: now[0])
    step = host.config_login({"url": cloud.url})
    assert host.login_status(step["login_id"]) == {"state": "pending"}
    assert host.cancel_sign_in(step["login_id"]) == {"ok": True}
    assert host.login_status(step["login_id"])["state"] == "cancelled"
    step = host.config_login({"url": cloud.url})
    now[0] = 21 * 60
    status = host.login_status(step["login_id"])
    assert status["state"] == "expired" and "zu lange" in status["message"]
    assert host.get_config()["values"]["password"] == ""


def test_wrong_app_password_is_an_error(make, cloud) -> None:
    host = make()
    step = host.sign_in({"url": cloud.url},
                        before_poll=lambda n: n == 0 and cloud.grant(app_password="Wrong-Pass"))
    assert step["state"] == "error"
    assert host.get_config()["values"]["password"] == ""
    host.assert_never_sent("Wrong-Pass", PASSWORD)


def test_a_server_without_the_flow(make, cloud) -> None:
    cloud.start_status = 404
    step = make().sign_in({"url": cloud.url})
    assert step["state"] == "error" and "Nextcloud-Anmeldung" in step["message"]


def test_user_name_is_optional_in_the_form_but_needed_by_hand(make, cloud) -> None:
    result = make().set_config({"url": f"{cloud.url}/remote.php/dav/files/{USER}/",
                                "password": PASSWORD})
    assert result == {"ok": False, "errors": {"username": "is required"}}
