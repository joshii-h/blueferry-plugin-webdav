"""A fake BlueFerry core for the 1.2 surfaces, strictly after the spec.

PLUGIN-SURFACES v1.2 (card, share, notify): it calls the D-Bus members the
way the core would (async callbacks, a sender), validates every reply
field by field and records the content-free ``CardChanged()`` and the
``Notify(...)`` signals. ``refetch`` makes it re-read the card on every
``CardChanged()``, like the core does, and keeps the snapshots.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.parse
from pathlib import Path

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_REPLY_BYTES = 512 * 1024


class ContractError(AssertionError):
    pass


def _text(value, limit, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or len(value) > limit:
        raise ContractError(f"text over {limit}: {value!r}")
    if any(not ch.isprintable() for ch in value):
        raise ContractError(f"control characters: {value!r}")


class FakeHost:
    def __init__(self, service, *, cache_root: Path, refetch: bool = False) -> None:
        self.service = service
        self.cache_root = cache_root
        self.refetch = refetch
        self.card_changes = 0
        self.snapshots: list[list[dict]] = []
        self.notifications: list[tuple[str, str, str, str, str]] = []
        service.CardChanged = self._card_changed
        service.Notify = self._notify

    # ---- signals -------------------------------------------------------------

    def _card_changed(self) -> None:
        self.card_changes += 1
        if self.refetch:
            self.snapshots.append(self.card_items())

    def _notify(self, *args) -> None:
        if len(args) != 5 or not all(isinstance(a, str) for a in args):
            raise ContractError("Notify takes five strings")
        title, body, _icon, label, action = args
        _text(title, 80)
        _text(body, 160)
        _text(label, 40)
        if action and not ID.fullmatch(action):
            raise ContractError("bad notify action id")
        self.notifications.append(args)

    # ---- calls ---------------------------------------------------------------

    def _call(self, method: str, *args) -> str:
        outcome: dict = {}
        getattr(self.service, method)(
            *args,
            reply=lambda value: outcome.setdefault("reply", value),
            error=lambda failure: outcome.setdefault("error", failure),
            sender=":1.core",
        )
        if "error" in outcome:
            raise outcome["error"]
        return self._checked(outcome["reply"])

    @staticmethod
    def _checked(reply) -> str:
        if not isinstance(reply, str) or len(reply.encode()) > MAX_REPLY_BYTES:
            raise ContractError("reply is not a string within 512 KiB")
        return reply

    def info(self) -> dict:
        return json.loads(self._checked(self.service.GetInfo(sender=":1.core")))

    def status(self) -> dict:
        return json.loads(self._checked(self.service.Status(sender=":1.core")))

    def get_config(self) -> dict:
        return json.loads(self._call("GetConfig"))

    def set_config(self, values: dict) -> dict:
        return json.loads(self._call("SetConfig", json.dumps(values)))

    def card_items(self) -> list[dict]:
        data = json.loads(self._call("GetCardItems"))
        if set(data) != {"items"} or not isinstance(data["items"], list):
            raise ContractError("card reply must be {items: [...]}")
        items = data["items"]
        if len(items) > 8:
            raise ContractError("more than 8 items")
        for item in items:
            if set(item) != {"id", "icon", "title", "subtitle", "actions"}:
                raise ContractError(f"card item keys: {sorted(item)}")
            if not ID.fullmatch(item["id"]):
                raise ContractError("bad item id")
            _text(item["icon"], 64)
            _text(item["title"], 80)
            _text(item["subtitle"], 160, optional=True)
            if not isinstance(item["actions"], list) or len(item["actions"]) > 3:
                raise ContractError("at most 3 actions")
            for action in item["actions"]:
                if set(action) != {"id", "label", "icon", "kind"}:
                    raise ContractError(f"action keys: {sorted(action)}")
                if not ID.fullmatch(action["id"]) or action["kind"] not in ("button", "primary"):
                    raise ContractError("bad action")
                _text(action["label"], 40)
                _text(action["icon"], 64, optional=True)
        return items

    def invoke(self, item_id: str, action_id: str, args: str = "{}") -> dict:
        result = json.loads(self._call("InvokeAction", item_id, action_id, args))
        if set(result) != {"ok", "message", "open_uri"} or not isinstance(result["ok"], bool):
            raise ContractError("InvokeAction reply must be {ok, message, open_uri}")
        _text(result["message"], 160, optional=True)
        if result["open_uri"] is not None:
            self._check_uri(result["open_uri"])
        return result

    def _check_uri(self, uri: str) -> None:
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme in ("http", "https") and parts.netloc:
            return
        if parts.scheme != "file":
            raise ContractError(f"open_uri scheme {parts.scheme!r}")
        path = Path(urllib.parse.unquote(parts.path)).resolve()
        if not path.is_relative_to(self.cache_root.resolve()):
            raise ContractError("file:// outside the plugin cache")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ContractError("file:// must be the user's regular file")

    def share_targets(self) -> list[dict]:
        data = json.loads(self._checked(self.service.ShareTargets(sender=":1.core")))
        targets = data["targets"]
        for target in targets:
            if set(target) != {"id", "label", "icon"}:
                raise ContractError("share target keys")
            _text(target["label"], 40)
        return targets

    def send_files(self, target_id: str, paths: list[str]) -> dict:
        result = json.loads(self._call("SendFiles", target_id, paths))
        if set(result) != {"ok", "message", "job"} or not isinstance(result["ok"], bool):
            raise ContractError("SendFiles reply must be {ok, message, job}")
        _text(result["message"], 160, optional=True)
        return result

    def click_notification(self, index: int = -1) -> dict:
        """The user clicks the action of a shown notification."""
        action = self.notifications[index][4]
        return self.invoke("notify", action, "{}")
