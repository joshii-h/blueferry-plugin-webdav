"""BlueFerry plugin: send files to a WebDAV share (Nextcloud, SFTPGo, ...).

Imports only ``blueferry.plugin_api`` from BlueFerry.
"""
from __future__ import annotations

import dataclasses
import re
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blueferry.plugin_api.manifest import PluginManifest

PLUGIN_ID = "io.weirdware.blueferry.webdav"
__version__ = "0.1.2"
# The surfaces of plugin contract 1.2 this plugin implements.
CAPABILITIES = ("card", "share", "notify")


def manifest_text() -> str:
    return (
        resources.files(__name__).joinpath(f"{PLUGIN_ID}.plugin").read_text(encoding="utf-8")
    )


def load_manifest(text: str | None = None) -> PluginManifest:
    """The validated manifest, also with a ``blueferry-plugin-api`` before 1.2.

    An older plugin_api drops the 1.2 capabilities (card, share, notify) and
    then rejects the manifest. The plugin process still needs its parsed
    settings schema, so it validates everything else and keeps its own
    capability list. Clients use their own parser and ignore the plugin
    until they understand 1.2, which is the intended behaviour.
    """
    from blueferry.plugin_api import KNOWN_CAPABILITIES
    from blueferry.plugin_api.manifest import parse_manifest

    text = manifest_text() if text is None else text
    if set(CAPABILITIES) <= set(KNOWN_CAPABILITIES):
        return parse_manifest(text)
    known = sorted(KNOWN_CAPABILITIES)[0]
    shim = re.sub(r"^Capabilities=.*$", f"Capabilities={known};", text, count=1, flags=re.M)
    return dataclasses.replace(parse_manifest(shim), capabilities=CAPABILITIES)
