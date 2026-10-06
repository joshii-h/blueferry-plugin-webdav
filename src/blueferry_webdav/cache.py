"""Owner-only download cache below ``$XDG_CACHE_HOME/blueferry/webdav``.

"Open" downloads a file here and hands the core a ``file://`` URI, which
the desktop opens with its default handler. So the cache keeps files
owner-only and not executable, and refuses types that a desktop would run
rather than show. The least recently used files go first.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

BUDGET_BYTES = 1024 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Launchers, installers and scripts: opening them may run code.
BLOCKED_EXTENSIONS = frozenset({
    ".desktop", ".sh", ".bash", ".zsh", ".csh", ".fish", ".command", ".run", ".bin",
    ".appimage", ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".js",
    ".jar", ".py", ".pl", ".rb", ".php", ".lnk", ".url", ".flatpakref", ".flatpakrepo",
    ".deb", ".rpm", ".kwinscript", ".plasmoid", ".service", ".so",
})


def default_root() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(cache_home) / "blueferry" / "webdav"


def blocked(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in BLOCKED_EXTENSIONS


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("cache directory has the wrong owner or type")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


class DownloadCache:
    def __init__(self, root: Path | None = None, *, budget: int = BUDGET_BYTES) -> None:
        self.root = root or default_root()
        self.budget = budget
        self._lock = threading.Lock()

    def _files(self) -> Path:
        _private_dir(self.root.parent)
        _private_dir(self.root)
        return _private_dir(self.root / "files")

    def path(self, entry_id: str, name: str) -> Path:
        if not _ID.fullmatch(entry_id):
            raise ValueError("invalid entry id")
        if "/" in name or name in ("", ".", ".."):
            raise ValueError("invalid file name")
        return self._files() / entry_id / name

    def cached(self, entry_id: str, name: str, size: int) -> Path | None:
        path = self.path(entry_id, name)
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return path

    def store(self, entry_id: str, name: str, writer: Callable[[IO[bytes]], object]) -> Path:
        path = self.path(entry_id, name)
        folder = _private_dir(path.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=".part-", dir=folder)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                writer(stream)
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            Path(temporary).unlink(missing_ok=True)
        self.prune(keep=path)
        return path

    def prune(self, *, keep: Path | None = None) -> None:
        with self._lock:
            files: list[tuple[float, int, Path]] = []
            for directory, _subdirs, names in os.walk(self._files()):
                for name in names:
                    path = Path(directory) / name
                    try:
                        info = os.lstat(path)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        files.append((info.st_mtime, info.st_size, path))
            total = sum(size for _mtime, size, _path in files)
            for _mtime, size, path in sorted(files):
                if total <= self.budget:
                    break
                if path == keep:
                    continue
                path.unlink(missing_ok=True)
                total -= size
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
