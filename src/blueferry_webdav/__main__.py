"""``blueferry-webdav serve|status|forget``.

Settings are made in BlueFerry (Plugins > WebDAV files > Settings, or
``blueferry plugins config io.weirdware.blueferry.webdav``).
"""
from __future__ import annotations

import argparse
import logging
import sys

from blueferry_webdav import load_manifest

ENTRY_POINT = "blueferry-webdav"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ENTRY_POINT, description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve on the session bus (started by D-Bus)")
    commands.add_parser("status", help="show whether the plugin is configured")
    commands.add_parser("forget", help="remove the stored password and settings")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from blueferry.plugin_api.manifest import ManifestError

    from blueferry_webdav.settings import SettingsError, SettingsStore

    if args.command == "forget":
        SettingsStore().forget()
        print("Removed the stored WebDAV settings and password.")
        return 0
    if args.command == "status":
        try:
            settings = SettingsStore().load()
        except SettingsError as error:
            print(error)
            return 1
        print(f"Configured for {settings.url} as {settings.username}"
              if settings else "Not configured.")
        return 0
    try:
        manifest = load_manifest()
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 1
    from blueferry.plugin_api.service import run

    from blueferry_webdav.service import WebDavService

    return run(lambda bus: WebDavService(manifest, bus))


if __name__ == "__main__":
    sys.exit(main())
