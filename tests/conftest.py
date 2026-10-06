"""Keep tests away from the user's configuration, cache, keyring and bus.

Server: a real WebDAV server (wsgidav) on 127.0.0.1 with a random port.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from cheroot import wsgi

_scratch = tempfile.mkdtemp(prefix="blueferry-webdav-tests-")
for _variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
    os.environ[_variable] = os.path.join(_scratch, _variable.lower())
    os.makedirs(os.environ[_variable], mode=0o700, exist_ok=True)
os.environ["XDG_DATA_DIRS"] = os.path.join(_scratch, "system")
# No test may reach a real bus or clipboard: the plugin runs in-process.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/blueferry-webdav-tests"
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ.pop("DISPLAY", None)

USER = "alice"
PASSWORD = "Pa55-w0rd-not-for-logs"


class Server:
    """A cheroot WSGI server on 127.0.0.1 with a random port, in a thread."""

    def __init__(self, app) -> None:
        self._server = wsgi.Server(("127.0.0.1", 0), app, numthreads=4)
        self._server.prepare()
        self.server_port = self._server.bind_addr[1]
        self._thread = threading.Thread(target=self._server.serve, daemon=True)
        self._thread.start()
        self._stopped = False

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._server.stop()
            self._thread.join(5)
            time.sleep(0.05)


def serve(app) -> Server:
    return Server(app)


class FakeSecret:
    """Stands in for gi.repository.Secret."""

    COLLECTION_DEFAULT = "default"

    class SchemaFlags:
        NONE = 0

    class SchemaAttributeType:
        STRING = 0

    class Schema:
        @staticmethod
        def new(name, _flags, _attributes):
            return name

    def __init__(self) -> None:
        self.items: dict = {}

    @staticmethod
    def _key(schema, attributes):
        return schema, tuple(sorted(attributes.items()))

    def password_store_sync(self, schema, attributes, _collection, _label, value, _cancel):
        self.items[self._key(schema, attributes)] = value
        return True

    def password_lookup_sync(self, schema, attributes, _cancel):
        return self.items.get(self._key(schema, attributes))

    def password_clear_sync(self, schema, attributes, _cancel):
        return self.items.pop(self._key(schema, attributes), None) is not None


@pytest.fixture
def dav_server(tmp_path):
    from wsgidav.wsgidav_app import WsgiDAVApp

    root = tmp_path / "dav-root"
    root.mkdir()
    app = WsgiDAVApp({
        "host": "127.0.0.1",
        "port": 0,
        "provider_mapping": {"/dav": str(root)},
        "simple_dc": {"user_mapping": {"*": {USER: {"password": PASSWORD}}}},
        "http_authenticator": {
            "accept_basic": True, "accept_digest": False, "default_to_digest": False,
            "domain_controller": None,
        },
        "verbose": 0,
        "logging": {"enable": False},
        "property_manager": True,
        "lock_storage": True,
    })
    logging.getLogger("wsgidav").setLevel(logging.ERROR)
    server = serve(app)
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{server.server_port}/dav/", root=root, server=server,
    )
    server.stop()
