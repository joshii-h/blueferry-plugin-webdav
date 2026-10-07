"""A fake Nextcloud below ``/nc``: WebDAV files, chunked upload v2, OCS shares.

Follows the documented protocol closely enough to catch mistakes:

* ``MKCOL /remote.php/dav/uploads/<user>/<id>`` needs a ``Destination``.
* ``PUT .../<id>/<n>`` needs ``Destination`` and ``OC-Total-Length``; every
  chunk but the last has at least ``min_chunk`` bytes.
* ``MOVE .../<id>/.file`` assembles the chunks in name order into the
  ``Destination`` and checks ``OC-Total-Length``.
* ``POST /ocs/v2.php/apps/files_sharing/api/v1/shares`` needs
  ``OCS-APIRequest: true`` and creates a link share for an existing path.
"""
from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from email.utils import formatdate

PREFIX = "/nc"


class FakeNextcloud:
    def __init__(self, user: str, password: str, *, min_chunk: int) -> None:
        self.user = user
        self.password = password
        self.min_chunk = min_chunk
        self.files: dict[str, bytes] = {}       # "/BlueFerry/a.txt" -> data
        self.mtimes: dict[str, float] = {}
        self.dirs: set[str] = {"/"}
        self.uploads: dict[str, dict] = {}       # id -> {"dest": str, "chunks": {name: bytes}}
        self.shares: list[dict] = []
        self.log: list[tuple[str, str]] = []
        self.fail_chunk: int | None = None       # answer 507 to this chunk number
        self.aliases: set[str] = set()           # other login names (e-mail login)
        self.clock = 1_760_000_000.0
        self._lock = threading.Lock()
        self.base = ""
        self.url = ""

    # ---- WSGI ----------------------------------------------------------------

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = urllib.parse.unquote(environ.get("PATH_INFO", ""))
        with self._lock:
            self.log.append((method, path))
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        if path == f"{PREFIX}/status.php":
            return self._reply(start_response, 200, json.dumps({
                "installed": True, "productname": "Nextcloud", "version": "30.0.1",
            }).encode(), "application/json")
        if not self._authorized(environ):
            return self._reply(start_response, 401, b"", headers=[
                ("WWW-Authenticate", 'Basic realm="Nextcloud"'),
            ])
        if path.rstrip("/") == f"{PREFIX}/remote.php/dav" and method == "PROPFIND":
            principal = f"{PREFIX}/remote.php/dav/principals/users/{self.user}/"
            xml = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
                   f"<d:href>{PREFIX}/remote.php/dav/</d:href><d:propstat><d:prop>"
                   f"<d:current-user-principal><d:href>{principal}</d:href>"
                   "</d:current-user-principal></d:prop>"
                   "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                   "</d:multistatus>").encode()
            return self._reply(start_response, 207, xml, "application/xml")
        files = f"{PREFIX}/remote.php/dav/files/{self.user}"
        uploads = f"{PREFIX}/remote.php/dav/uploads/{self.user}/"
        with self._lock:
            if path.startswith(files):
                return self._files(environ, start_response, method, path[len(files):] or "/",
                                   body)
            if path.startswith(uploads):
                return self._uploads(environ, start_response, method, path[len(uploads):], body)
            if path == f"{PREFIX}/ocs/v2.php/apps/files_sharing/api/v1/shares":
                return self._share(environ, start_response, method, body)
        return self._reply(start_response, 404, b"")

    def _authorized(self, environ) -> bool:
        header = environ.get("HTTP_AUTHORIZATION", "")
        return any(
            header == "Basic " + base64.b64encode(f"{name}:{self.password}".encode()).decode()
            for name in {self.user, *self.aliases}
        )

    @staticmethod
    def _reply(start_response, status, body=b"", content_type="text/plain", headers=()):
        reason = {200: "OK", 201: "Created", 204: "No Content", 207: "Multi-Status",
                  400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                  405: "Method Not Allowed", 409: "Conflict", 412: "Precondition Failed",
                  507: "Insufficient Storage"}.get(status, "X")
        start_response(f"{status} {reason}", [
            ("Content-Type", content_type), ("Content-Length", str(len(body))), *headers,
        ])
        return [body]

    def _dest(self, environ) -> str | None:
        destination = environ.get("HTTP_DESTINATION", "")
        prefix = f"{self.base}{PREFIX}/remote.php/dav/files/{self.user}"
        if not destination.startswith(prefix):
            return None
        return urllib.parse.unquote(destination[len(prefix):])

    # ---- files ---------------------------------------------------------------

    def _files(self, environ, start_response, method, path, body):
        parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
        if method == "MKCOL":
            folder = path.rstrip("/") or "/"
            if folder in self.dirs:
                return self._reply(start_response, 405)
            if parent not in self.dirs:
                return self._reply(start_response, 409)
            self.dirs.add(folder)
            return self._reply(start_response, 201)
        if method == "PUT":
            if parent not in self.dirs:
                return self._reply(start_response, 409)
            self._store(path, body)
            return self._reply(start_response, 201)
        if method in ("HEAD", "GET"):
            if path in self.files:
                data = self.files[path]
                return self._reply(start_response, 200, data if method == "GET" else b"",
                                   "application/octet-stream")
            return self._reply(start_response, 404)
        if method == "PROPFIND":
            return self._propfind(environ, start_response, path)
        return self._reply(start_response, 405)

    def _store(self, path: str, data: bytes) -> None:
        self.clock += 60
        self.files[path] = data
        self.mtimes[path] = self.clock

    def _propfind(self, environ, start_response, path):
        folder = path.rstrip("/") or "/"
        if folder not in self.dirs:
            return self._reply(start_response, 404)
        href_root = f"{PREFIX}/remote.php/dav/files/{self.user}"
        responses = [self._response(href_root + (folder if folder != "/" else "") + "/", None)]
        if environ.get("HTTP_DEPTH") == "1":
            for name in self.files:
                if name.rsplit("/", 1)[0] == (folder if folder != "/" else ""):
                    responses.append(self._response(href_root + name, name))
        xml = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
               + "".join(responses) + "</d:multistatus>").encode()
        return self._reply(start_response, 207, xml, "application/xml")

    def _response(self, href: str, name: str | None) -> str:
        quoted = urllib.parse.quote(href)
        if name is None:
            props = "<d:resourcetype><d:collection/></d:resourcetype>"
        else:
            props = (f"<d:resourcetype/><d:getcontentlength>{len(self.files[name])}"
                     f"</d:getcontentlength><d:getlastmodified>"
                     f"{formatdate(self.mtimes[name], usegmt=True)}</d:getlastmodified>"
                     "<d:getcontenttype>text/plain</d:getcontenttype>")
        return (f"<d:response><d:href>{quoted}</d:href><d:propstat><d:prop>{props}</d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    # ---- chunked upload v2 ---------------------------------------------------

    def _uploads(self, environ, start_response, method, rest, body):
        transfer, _, chunk = rest.partition("/")
        transfer = transfer.strip("/")
        if method == "MKCOL" and not chunk.strip("/"):
            if self._dest(environ) is None or transfer in self.uploads:
                return self._reply(start_response, 400)
            self.uploads[transfer] = {"dest": self._dest(environ), "chunks": {}}
            return self._reply(start_response, 201)
        upload = self.uploads.get(transfer)
        if upload is None:
            return self._reply(start_response, 404)
        if method == "DELETE":
            del self.uploads[transfer]
            return self._reply(start_response, 204)
        if method == "PUT":
            if self._dest(environ) != upload["dest"] or not environ.get("HTTP_OC_TOTAL_LENGTH"):
                return self._reply(start_response, 400)
            if self.fail_chunk is not None and int(chunk) == self.fail_chunk:
                return self._reply(start_response, 507)
            upload["chunks"][chunk] = body
            upload["total"] = int(environ["HTTP_OC_TOTAL_LENGTH"])
            return self._reply(start_response, 201)
        if method == "MOVE" and chunk == ".file":
            destination = self._dest(environ)
            if destination != upload["dest"]:
                return self._reply(start_response, 400)
            names = sorted(upload["chunks"])
            parts = [upload["chunks"][name] for name in names]
            if any(len(part) < self.min_chunk for part in parts[:-1]):
                return self._reply(start_response, 400)
            data = b"".join(parts)
            if len(data) != int(environ.get("HTTP_OC_TOTAL_LENGTH") or -1):
                return self._reply(start_response, 400)
            if environ.get("HTTP_OVERWRITE") == "F" and destination in self.files:
                return self._reply(start_response, 412)
            self._store(destination, data)
            del self.uploads[transfer]
            return self._reply(start_response, 201)
        return self._reply(start_response, 405)

    # ---- OCS -----------------------------------------------------------------

    def _share(self, environ, start_response, method, body):
        if method != "POST" or environ.get("HTTP_OCS_APIREQUEST") != "true":
            return self._reply(start_response, 400)
        form = dict(urllib.parse.parse_qsl(body.decode()))
        if form.get("shareType") != "3" or form.get("path") not in self.files:
            return self._reply(start_response, 404, json.dumps({
                "ocs": {"meta": {"status": "failure", "statuscode": 404}, "data": []},
            }).encode(), "application/json")
        token = f"tok{len(self.shares) + 1}"
        self.shares.append({**form, "token": token})
        return self._reply(start_response, 200, json.dumps({"ocs": {
            "meta": {"status": "ok", "statuscode": 200},
            "data": {"id": len(self.shares), "url": f"https://cloud.example.org/s/{token}"},
        }}).encode(), "application/json")
