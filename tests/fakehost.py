"""The kit's strict fake BlueFerry core, with the names these tests use.

:class:`blueferry_plugin_kit.testing.FakeHost` plays the core for the 1.2
surfaces (card, share, notify) and checks every reply against the spec.
This adapter keeps the single ``cache_root`` argument, the
``card_changes`` counter and adds ``info()``/``status()``, which the kit
host does not offer.
"""
from __future__ import annotations

import json
from pathlib import Path

from blueferry_plugin_kit import testing
from blueferry_plugin_kit.testing.host import MAX_REPLY_BYTES, SpecViolation


def _checked(reply) -> str:
    if not isinstance(reply, str) or len(reply.encode()) > MAX_REPLY_BYTES:
        raise SpecViolation("reply is not a string within 512 KiB")
    return reply


class FakeHost(testing.FakeHost):
    def __init__(self, service, *, cache_root: Path, refetch: bool = False) -> None:
        super().__init__(service, cache_roots=[cache_root], refetch=refetch)
        self.cache_root = cache_root

    @property
    def card_changes(self) -> int:
        return self.card_changed

    def info(self) -> dict:
        return json.loads(_checked(self.call("GetInfo")))

    def status(self) -> dict:
        return json.loads(_checked(self.call("Status")))
