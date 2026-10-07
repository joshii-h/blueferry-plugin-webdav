"""Keep tests away from the user's configuration, cache, keyring and bus.

Servers: a real WebDAV server (wsgidav) and a fake Nextcloud
(:mod:`fake_nextcloud`), both on 127.0.0.1 with a random port.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from blueferry_plugin_kit.testing import DavServer, WsgiServer, isolate_environment

# No test may reach a real bus or clipboard: the plugin runs in-process.
isolate_environment("blueferry-webdav-tests-", bus_name="blueferry-webdav-tests")
# The card and notification assertions are in German; one test switches.
for _variable in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
    os.environ.pop(_variable, None)
os.environ["LANG"] = "de_CH.UTF-8"

USER = "alice"
PASSWORD = "Pa55-w0rd-not-for-logs"


@pytest.fixture
def dav_server(tmp_path):
    server = DavServer(tmp_path / "dav-root", USER, PASSWORD)
    yield SimpleNamespace(url=server.url, root=server.root, server=server)
    server.stop()


@pytest.fixture
def nextcloud(monkeypatch):
    from blueferry_plugin_kit.dav import webdav
    from fake_nextcloud import FakeNextcloud

    chunk = 64 * 1024
    monkeypatch.setattr(webdav, "CHUNK_BYTES", chunk)
    fake = FakeNextcloud(USER, PASSWORD, min_chunk=chunk)
    server = WsgiServer(fake)
    fake.base = server.base
    fake.url = f"{fake.base}/nc/remote.php/dav/files/{USER}/"
    yield fake
    server.stop()
