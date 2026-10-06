"""Minimal WebDAV client with Nextcloud extras.

* ``PROPFIND`` (Depth 0 to check the login, Depth 1 to list a folder),
  ``MKCOL`` for the target folder, ``HEAD`` to avoid overwriting, ``PUT``
  streamed from the file, ``GET`` streamed into the cache.
* Nextcloud: chunked upload v2 (``MKCOL``/``PUT``/``MOVE`` below
  ``remote.php/dav/uploads/<user>/``) for large files and public links
  through the OCS sharing API.

Only https, except http to loopback and, when the user allows it, to hosts
on the local network (checked again against the resolved addresses before
each request). Redirects are refused (they would carry the password to
another URL), every request has a timeout, every response body a size
limit, and remote names are percent-encoded segment by segment. Errors are
short tokens; nothing from the server and no credentials are logged.
"""
from __future__ import annotations

import base64
import email.utils
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from blueferry_webdav import __version__

TIMEOUT_SEC = 30.0
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
# Nextcloud wants chunks of 5 MiB to 5 GiB (the last one may be smaller).
CHUNK_BYTES = 10 * 1024 * 1024
_BLOCK = 256 * 1024
_MAX_NAME_BYTES = 200
_LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
_NEXTCLOUD_PATH = re.compile(r"^(?P<root>.*?)/remote\.php/dav/files/(?P<user>[^/]+)/(?P<sub>.*)$")
_DAV = "{DAV:}"
Progress = Callable[[int], None]


class DavError(Exception):
    """``token`` is one of: unauthorized, forbidden, not-found, conflict,
    too-large, no-space, server-error, network, bad-response, redirect,
    invalid-url, insecure, exists."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


# ---- URLs and names -----------------------------------------------------------


def _private_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def lan_host(host: str) -> bool:
    """A host name or address that plausibly stays inside the LAN."""
    host = host.lower().rstrip(".")
    if _private_address(host):
        return True
    try:
        ipaddress.ip_address(host)
        return False  # a public address literal
    except ValueError:
        pass
    return bool(host) and ("." not in host or host.endswith(_LAN_SUFFIXES))


def normalize_base(raw: str, *, allow_http_lan: bool = False) -> str:
    """The WebDAV base URL with a trailing slash; raise DavError otherwise."""
    value = (raw or "").strip()
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise DavError("invalid-url") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or len(value) > 2048:
        raise DavError("invalid-url")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise DavError("invalid-url")
    if any(not ch.isprintable() or ch.isspace() for ch in value):
        raise DavError("invalid-url")
    if parts.scheme == "http" and not (_loopback(host) or (allow_http_lan and lan_host(host))):
        raise DavError("insecure")
    segments = [s for s in parts.path.split("/") if s]
    if any(urllib.parse.unquote(s) in (".", "..") for s in segments):
        raise DavError("invalid-url")
    path = "/" + "/".join(segments) + "/" if segments else "/"
    netloc = parts.netloc
    if port is None and ":" in netloc.rsplit("]", 1)[-1]:
        netloc = netloc.rstrip(":")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def safe_name(name: str) -> str:
    """A local file's name, made harmless as one remote path segment."""
    base = unicodedata.normalize("NFC", os.path.basename(str(name)))
    cleaned = "".join(
        "_" if ch in "/\\" or not ch.isprintable() else ch for ch in base
    ).strip().lstrip(".").rstrip(". ")
    if not cleaned:
        cleaned = "file"
    stem, extension = os.path.splitext(cleaned)
    if len(extension.encode()) > 20:
        stem, extension = cleaned, ""
    while len((stem + extension).encode()) > _MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem or "file") + extension


def folder_segments(folder: str) -> tuple[str, ...]:
    """The target folder as path segments; raise ValueError for traversal."""
    parts = [part.strip() for part in str(folder or "").replace("\\", "/").split("/")]
    segments = tuple(part for part in parts if part and part != ".")
    if len(segments) > 16:
        raise ValueError("too many folder levels")
    for segment in segments:
        if segment == ".." or any(not ch.isprintable() for ch in segment):
            raise ValueError("must not contain .. or control characters")
        if len(segment.encode()) > _MAX_NAME_BYTES:
            raise ValueError("a folder name is too long")
    return segments


def quote_path(segments: tuple[str, ...] | list[str]) -> str:
    return "".join(urllib.parse.quote(segment, safe="") + "/" for segment in segments)


def numbered(name: str, number: int) -> str:
    stem, extension = os.path.splitext(name)
    return f"{stem} ({number}){extension}"


# ---- listing ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    name: str           # decoded, single segment
    url: str            # absolute URL below the folder
    size: int
    modified: float     # POSIX time, 0 when unknown
    content_type: str


_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype/>"
    b"</d:prop></d:propfind>"
)


def _parse_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0.0


def parse_multistatus(data: bytes, folder_url: str) -> list[Entry]:
    """Files directly inside ``folder_url``; anything else is dropped."""
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise DavError("bad-response")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise DavError("bad-response") from None
    folder = urllib.parse.urlsplit(folder_url)
    folder_path = urllib.parse.unquote(folder.path)
    entries: list[Entry] = []
    for response in root.iter(f"{_DAV}response"):
        href = (response.findtext(f"{_DAV}href") or "").strip()
        if not href:
            continue
        target = urllib.parse.urlsplit(urllib.parse.urljoin(folder_url, href))
        if (target.scheme, target.netloc) != (folder.scheme, folder.netloc):
            continue
        path = urllib.parse.unquote(target.path)
        if not path.startswith(folder_path):
            continue
        name = path[len(folder_path):]
        if not name or "/" in name or name in (".", ".."):
            continue  # the folder itself, sub folders or something odd
        props: dict[str, ET.Element] = {}
        for propstat in response.iter(f"{_DAV}propstat"):
            status = propstat.findtext(f"{_DAV}status") or ""
            if " 200 " not in status + " ":
                continue
            for prop in propstat.iter(f"{_DAV}prop"):
                for child in prop:
                    props[child.tag] = child
        kind = props.get(f"{_DAV}resourcetype")
        if kind is not None and kind.find(f"{_DAV}collection") is not None:
            continue
        length = props.get(f"{_DAV}getcontentlength")
        try:
            size = int((length.text if length is not None else None) or 0)
        except ValueError:
            size = 0
        modified = props.get(f"{_DAV}getlastmodified")
        content_type = props.get(f"{_DAV}getcontenttype")
        entries.append(Entry(
            name=name,
            url=urllib.parse.urlunsplit((folder.scheme, folder.netloc, folder.path
                                         + urllib.parse.quote(name, safe=""), "", "")),
            size=max(0, size),
            modified=_parse_time(modified.text if modified is not None else None),
            content_type=(content_type.text or "")[:100] if content_type is not None else "",
        ))
    return entries


# ---- HTTP ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Nextcloud:
    root: str        # https://host[/sub] (no trailing slash)
    user: str        # the user segment as it appears in the URL (encoded)
    sub: tuple[str, ...]  # decoded path below the user's files root

    @property
    def uploads(self) -> str:
        return f"{self.root}/remote.php/dav/uploads/{self.user}/"


def nextcloud_layout(base_url: str) -> Nextcloud | None:
    parts = urllib.parse.urlsplit(base_url)
    match = _NEXTCLOUD_PATH.match(parts.path)
    if match is None:
        return None
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, match["root"], "", ""))
    sub = tuple(urllib.parse.unquote(s) for s in match["sub"].split("/") if s)
    return Nextcloud(root=root, user=match["user"], sub=sub)


def _status_token(code: int) -> str:
    return {
        401: "unauthorized", 403: "forbidden", 404: "not-found", 409: "conflict",
        412: "exists", 413: "too-large", 423: "forbidden", 507: "no-space",
    }.get(code, "redirect" if 300 <= code < 400 else "server-error")


class Response:
    """Status, headers and the (size-limited) body or a stream."""

    def __init__(self, status: int, headers: http.client.HTTPMessage, raw: Any) -> None:
        self.status = status
        self.headers = headers
        self.raw = raw

    def read(self, limit: int) -> bytes:
        data = self.raw.read(limit + 1)
        if len(data) > limit:
            raise DavError("too-large")
        return bytes(data)


class WebDavClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        allow_http_lan: bool = False,
        nextcloud: bool = False,
        timeout: float = TIMEOUT_SEC,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.base = normalize_base(base_url, allow_http_lan=allow_http_lan)
        self.allow_http_lan = allow_http_lan
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._auth = f"Basic {token}"
        self.timeout = timeout
        layout = nextcloud_layout(self.base)
        self.nextcloud = layout if nextcloud else None
        self.layout = layout
        self._resolve = resolve or _resolve

    def __repr__(self) -> str:  # never show the credentials
        return f"WebDavClient({self.base!r})"

    # ---- plumbing ------------------------------------------------------------

    def _check_target(self, parts: urllib.parse.SplitResult) -> None:
        base = urllib.parse.urlsplit(self.base)
        if (parts.scheme, parts.hostname, parts.port) != (base.scheme, base.hostname, base.port):
            raise DavError("invalid-url")  # credentials only ever go to the configured server
        host = (parts.hostname or "").lower()
        if parts.scheme == "http" and not _loopback(host):
            try:
                addresses = self._resolve(host)
            except OSError:
                raise DavError("network") from None
            if not addresses or not all(_private_address(a) for a in addresses):
                raise DavError("insecure")

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        source: IO[bytes] | None = None,
        length: int | None = None,
        progress: Progress | None = None,
    ) -> Response:
        """Send one request; the caller closes the response with ``close()``."""
        parts = urllib.parse.urlsplit(url)
        self._check_target(parts)
        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname or "", parts.port, timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parts.hostname or "", parts.port, timeout=self.timeout,
            )
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            connection.putrequest(method, path or "/", skip_accept_encoding=True)
            all_headers = {
                "Authorization": self._auth,
                "User-Agent": f"blueferry-webdav/{__version__}",
                **(headers or {}),
            }
            if body is not None:
                length = len(body)
            if body is not None or source is not None:
                all_headers["Content-Length"] = str(length or 0)
            for key, value in all_headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            if body is not None:
                connection.send(body)
            elif source is not None:
                remaining = length or 0
                while remaining > 0:
                    block = source.read(min(_BLOCK, remaining))
                    if not block:
                        raise DavError("bad-response")  # the file shrank meanwhile
                    connection.send(block)
                    remaining -= len(block)
                    if progress is not None:
                        progress(len(block))
            response = connection.getresponse()
        except DavError:
            connection.close()
            raise
        except (OSError, http.client.HTTPException, ValueError):
            connection.close()
            raise DavError("network") from None
        result = Response(response.status, response.headers, response)
        result.close = connection.close  # type: ignore[attr-defined]
        return result

    def _simple(
        self, method: str, url: str, ok: tuple[int, ...], *, limit: int = MAX_XML_BYTES,
        **kwargs: Any,
    ) -> tuple[int, bytes]:
        response = self.request(method, url, **kwargs)
        try:
            if response.status not in ok:
                raise DavError(_status_token(response.status))
            return response.status, response.read(limit)
        except (OSError, http.client.HTTPException):
            raise DavError("network") from None
        finally:
            response.close()  # type: ignore[attr-defined]

    # ---- operations ----------------------------------------------------------

    def folder_url(self, segments: tuple[str, ...]) -> str:
        return self.base + quote_path(segments)

    def check(self) -> None:
        """Log in with Depth 0 on the base URL."""
        self._simple("PROPFIND", self.base, (207,), headers={
            "Depth": "0", "Content-Type": "application/xml; charset=utf-8",
        }, body=_PROPFIND_BODY)

    def detect_nextcloud(self) -> bool:
        """URL layout of Nextcloud and ``status.php`` saying so."""
        if self.layout is None:
            return False
        try:
            _status, data = self._simple(
                "GET", self.layout.root + "/status.php", (200,), limit=MAX_JSON_BYTES,
                headers={"Accept": "application/json"},
            )
            info = json.loads(data)
        except (DavError, ValueError):
            return False
        return (
            isinstance(info, dict) and info.get("installed") is True
            and "nextcloud" in str(info.get("productname", "Nextcloud")).lower()
        )

    def ensure_folder(self, segments: tuple[str, ...]) -> str:
        url = self.base
        for segment in segments:
            url += urllib.parse.quote(segment, safe="") + "/"
            self._simple("MKCOL", url, (201, 405))
        return url

    def exists(self, url: str) -> bool:
        response = self.request("HEAD", url)
        try:
            if response.status == 404:
                return False
            if 200 <= response.status < 300:
                return True
            raise DavError(_status_token(response.status))
        finally:
            response.close()  # type: ignore[attr-defined]

    def free_name(self, folder_url: str, name: str) -> str:
        for number in range(1, 100):
            candidate = name if number == 1 else numbered(name, number)
            if not self.exists(folder_url + urllib.parse.quote(candidate, safe="")):
                return candidate
        raise DavError("exists")

    def upload(
        self,
        source: IO[bytes],
        size: int,
        folder_url: str,
        name: str,
        *,
        progress: Progress | None = None,
    ) -> str:
        """Store ``size`` bytes from ``source`` as ``name``; return its URL."""
        target = folder_url + urllib.parse.quote(name, safe="")
        if self.nextcloud is not None and size > CHUNK_BYTES:
            self._upload_chunked(source, size, target, progress)
        else:
            self._simple(
                "PUT", target, (200, 201, 204), source=source, length=size, progress=progress,
                headers={"Content-Type": "application/octet-stream"},
            )
        return target

    def _upload_chunked(
        self, source: IO[bytes], size: int, target: str, progress: Progress | None,
    ) -> None:
        assert self.nextcloud is not None
        folder = self.nextcloud.uploads + "blueferry-" + secrets.token_hex(12) + "/"
        common = {"Destination": target, "OC-Total-Length": str(size)}
        self._simple("MKCOL", folder, (201,), headers={"Destination": target})
        try:
            offset, index = 0, 1
            while offset < size:
                length = min(CHUNK_BYTES, size - offset)
                self._simple(
                    "PUT", f"{folder}{index:05d}", (200, 201, 204), source=source,
                    length=length, progress=progress, headers=dict(common),
                )
                offset += length
                index += 1
            self._simple("MOVE", folder + ".file", (200, 201, 204), headers={
                **common, "Overwrite": "F",
            })
        except DavError:
            try:
                self._simple("DELETE", folder, (200, 204, 404))
            except DavError:
                pass
            raise

    def list_folder(self, segments: tuple[str, ...]) -> list[Entry]:
        url = self.folder_url(segments)
        try:
            _status, data = self._simple("PROPFIND", url, (207,), headers={
                "Depth": "1", "Content-Type": "application/xml; charset=utf-8",
            }, body=_PROPFIND_BODY)
        except DavError as error:
            if error.token == "not-found":
                return []
            raise
        return parse_multistatus(data, url)

    def download(self, url: str, target: IO[bytes], max_bytes: int) -> int:
        if not url.startswith(self.base):
            raise DavError("invalid-url")
        response = self.request("GET", url)
        written = 0
        try:
            if response.status != 200:
                raise DavError(_status_token(response.status))
            while True:
                block = response.raw.read(_BLOCK)
                if not block:
                    return written
                written += len(block)
                if written > max_bytes:
                    raise DavError("too-large")
                target.write(block)
        except (OSError, http.client.HTTPException):
            raise DavError("network") from None
        finally:
            response.close()  # type: ignore[attr-defined]

    # ---- Nextcloud ----------------------------------------------------------

    def share_link(self, segments: tuple[str, ...], name: str) -> str:
        """A read-only public link through OCS; Nextcloud only."""
        if self.nextcloud is None:
            raise DavError("not-found")
        path = "/" + "/".join((*self.nextcloud.sub, *segments, name))
        body = urllib.parse.urlencode({"path": path, "shareType": "3", "permissions": "1"})
        _status, data = self._simple(
            "POST", self.nextcloud.root + "/ocs/v2.php/apps/files_sharing/api/v1/shares",
            (200,), limit=MAX_JSON_BYTES, body=body.encode(), headers={
                "OCS-APIRequest": "true", "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            link = json.loads(data)["ocs"]["data"]["url"]
        except (ValueError, KeyError, TypeError):
            raise DavError("bad-response") from None
        if not isinstance(link, str) or not re.fullmatch(r"https?://[^\s]{1,2000}", link):
            raise DavError("bad-response")
        return link

    def web_folder_url(self, segments: tuple[str, ...]) -> str | None:
        """Nextcloud's Files app for the folder; ``None`` elsewhere."""
        if self.nextcloud is None:
            return None
        path = "/" + "/".join((*self.nextcloud.sub, *segments))
        return self.nextcloud.root + "/index.php/apps/files/?dir=" + urllib.parse.quote(path)


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
