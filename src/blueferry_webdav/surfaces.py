"""Plugin contract 1.2 surfaces: ``card``, ``share`` and ``notify``.

Implements PLUGIN-SURFACES v1.2 on top of ``blueferry.plugin_api``'s
:class:`PluginService`. As the spec binds it, every v1.2 method and signal
(GetCardItems, CardChanged, InvokeAction, ShareTargets, SendFiles, Notify)
lives on the existing ``io.weirdware.BlueFerry.Plugin1`` interface at the
plugin's object path; the manifest's capabilities tell the core which of
them to call. Once ``blueferry-plugin-api`` ships its own helpers
(``CardItem``, ``emit_card_changed()``, ...) this module can use them.

Everything sent to the core is plain text, clipped to the limits of the
spec (title 80, subtitle 160, label 40 characters, at most 8 items with 3
actions each), so a long file name never makes the core reject the card.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import blueferry.plugin_api as api
import dbus
import dbus.service
from blueferry.plugin_api import PLUGIN_INTERFACE
from blueferry.plugin_api.service import PluginService

SURFACES_MINOR = 2

MAX_ITEMS = 8
MAX_ACTIONS = 3
MAX_TITLE = 80
MAX_SUBTITLE = 160
MAX_LABEL = 40
MAX_ARGS_BYTES = 16 * 1024
MAX_PATHS = 100
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ICON = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
NOTIFY_ITEM = "notify"


def plain(text: object, limit: int) -> str:
    """One line of plain text, at most ``limit`` characters."""
    value = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _icon(name: str | None) -> str | None:
    return name if name and _ICON.fullmatch(name) else None


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    label: str
    icon: str | None = None
    kind: str = "button"  # "button" or "primary"

    def as_json(self) -> dict[str, object]:
        if not ITEM_ID.fullmatch(self.id):
            raise ValueError("invalid action id")
        return {
            "id": self.id,
            "label": plain(self.label, MAX_LABEL),
            "icon": _icon(self.icon),
            "kind": "primary" if self.kind == "primary" else "button",
        }


@dataclass(frozen=True, slots=True)
class CardItem:
    id: str
    icon: str
    title: str
    subtitle: str | None = None
    actions: Sequence[Action] = field(default_factory=tuple)

    def as_json(self) -> dict[str, object]:
        if not ITEM_ID.fullmatch(self.id):
            raise ValueError("invalid item id")
        return {
            "id": self.id,
            "icon": _icon(self.icon) or "application-x-addon",
            "title": plain(self.title, MAX_TITLE),
            "subtitle": None if self.subtitle is None else plain(self.subtitle, MAX_SUBTITLE),
            "actions": [action.as_json() for action in list(self.actions)[:MAX_ACTIONS]],
        }


@dataclass(frozen=True, slots=True)
class ShareTarget:
    id: str
    label: str
    icon: str

    def as_json(self) -> dict[str, object]:
        return {"id": self.id, "label": plain(self.label, MAX_LABEL),
                "icon": _icon(self.icon) or "document-send"}


def action_result(ok: bool, message: str | None = None, open_uri: str | None = None) -> str:
    return json.dumps({
        "ok": bool(ok),
        "message": None if message is None else plain(message, MAX_SUBTITLE),
        "open_uri": open_uri,
    })


class SurfacesService(PluginService):
    """Card, Share and Notify; subclasses implement the hooks."""

    # ---- hooks -------------------------------------------------------------

    def card_items(self) -> list[CardItem]:
        """Blocking; worker thread."""
        return []

    def invoke_action(self, item_id: str, action_id: str, args: dict[str, Any]) -> str:
        """Blocking; worker thread. Return :func:`action_result`."""
        return action_result(False, "unknown action")

    def share_targets(self) -> list[ShareTarget]:
        """Main thread; must be quick."""
        return []

    def send_files(self, target_id: str, paths: list[str]) -> str:
        """Blocking; worker thread. Return ``{"ok", "message", "job"}`` JSON."""
        return json.dumps({"ok": False, "message": "not supported", "job": None})

    # ---- helpers -----------------------------------------------------------

    def emit_card_changed(self) -> None:
        """Thread-safe: tell the core to call GetCardItems again."""
        self._to_main(self.CardChanged)

    def emit_notify(
        self, title: str, body: str, icon: str, action_label: str = "", action_id: str = "",
    ) -> None:
        """Thread-safe: ask the core for a desktop notification."""
        if action_id and not ITEM_ID.fullmatch(action_id):
            raise ValueError("invalid action id")
        values = (
            plain(title, MAX_TITLE), plain(body, MAX_SUBTITLE), _icon(icon) or "",
            plain(action_label, MAX_LABEL) if action_id else "", action_id,
        )
        self._to_main(lambda: self.Notify(*values))

    def start_job(self, work: Any) -> None:
        """Run ``work`` on its own worker; the service does not idle out meanwhile.

        Called from a worker; the in-flight count changes on the main loop,
        queued before the calling method's reply, so it never drops to zero
        in between.
        """
        self._to_main(self._job_started)

        def runner() -> None:
            try:
                work()
            finally:
                self._to_main(self._job_finished)

        self._start_worker(runner)

    def _job_started(self) -> None:
        self.in_flight += 1

    def _job_finished(self) -> None:
        self.in_flight -= 1
        self.last_activity = self._clock()

    # ---- Plugin1 (reports contract 1.2 with an older plugin_api) ------------

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s", sender_keyword="sender",
    )
    def GetInfo(self, sender=None) -> str:
        self.admit(sender)
        return json.dumps({
            "id": self.manifest.id,
            "name": self.manifest.name,
            "version": self.manifest.version,
            "api_version": api.API_VERSION,
            "api_minor": max(api.API_MINOR, SURFACES_MINOR),
            "capabilities": list(self.manifest.capabilities),
        })

    # ---- Card --------------------------------------------------------------

    def _card_json(self) -> str:
        items = [item.as_json() for item in self.card_items()[:MAX_ITEMS]]
        return json.dumps({"items": items})

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def GetCardItems(self, reply, error, sender=None) -> None:
        self.admit(sender)
        self.run_async(self._card_json, reply, error)

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="sss", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def InvokeAction(self, item_id, action_id, args_json, reply, error, sender=None) -> None:
        self.admit(sender)
        item, action, raw = str(item_id), str(action_id), str(args_json)

        def work() -> str:
            if not (ITEM_ID.fullmatch(item) and ITEM_ID.fullmatch(action)):
                return action_result(False, "unknown action")
            if len(raw.encode("utf-8", "surrogatepass")) > MAX_ARGS_BYTES:
                return action_result(False, "arguments too large")
            try:
                args = json.loads(raw or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                return action_result(False, "arguments must be a JSON object")
            return self.invoke_action(item, action, args)

        self.run_async(work, reply, error)

    @dbus.service.signal(PLUGIN_INTERFACE, signature="")
    def CardChanged(self) -> None:
        """Content-free: the core calls GetCardItems again."""

    # ---- Share -------------------------------------------------------------

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="", out_signature="s", sender_keyword="sender",
    )
    def ShareTargets(self, sender=None) -> str:
        self.admit(sender)
        return json.dumps({"targets": [target.as_json() for target in self.share_targets()]})

    @dbus.service.method(
        PLUGIN_INTERFACE, in_signature="sas", out_signature="s",
        async_callbacks=("reply", "error"), sender_keyword="sender",
    )
    def SendFiles(self, target_id, paths, reply, error, sender=None) -> None:
        self.admit(sender)
        target = str(target_id)
        files = [str(path) for path in list(paths)[: MAX_PATHS + 1]]

        def work() -> str:
            if len(files) > MAX_PATHS:
                return json.dumps({
                    "ok": False, "message": f"at most {MAX_PATHS} files at once", "job": None,
                })
            return self.send_files(target, files)

        self.run_async(work, reply, error)

    # ---- Notify ------------------------------------------------------------

    @dbus.service.signal(PLUGIN_INTERFACE, signature="sssss")
    def Notify(self, title, body, icon, action_label, action_id) -> None:
        """The core shows a desktop notification under its policy."""
