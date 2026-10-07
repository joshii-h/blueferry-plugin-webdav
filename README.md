# blueferry-plugin-webdav

Send files from BlueFerry to your own WebDAV storage: Nextcloud, SFTPGo or
any other WebDAV server.

A plugin for [BlueFerry](https://github.com/joshii-h/blueferry). It adds:

- a **share target** "Ablage (WebDAV)" to BlueFerry's "Send to…" (card
  tools, tray, terminal client, `blueferry send <file> --to webdav`).
  Files are uploaded with `PUT`; on Nextcloud, files over 10 MB use
  Nextcloud's chunked upload (v2). Progress shows on the phone card.
- a **notification** "Hochgeladen" after each upload, with "Link kopieren"
  (Nextcloud public link, when enabled) or "Ordner öffnen".
- a **card** "Zuletzt hochgeladen" with the five newest files in the target
  folder. "Öffnen" downloads a file into the plugin cache and opens it;
  "Aktualisieren" reloads the list.

It runs as its own process on the session bus and implements the plugin
contract 1.2 (`card`, `share`, `notify`; see `PLUGINS.md` in the BlueFerry
repository). It needs a BlueFerry that understands contract 1.2; older
versions ignore the plugin.

## Install

```sh
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-webdav
```

or pick "WebDAV files" in BlueFerry's settings under Plugins. BlueFerry
shows the source, version tag and commit, the capabilities and the command
it will run, and installs into its own virtual environment only after you
confirm.

## Configure

In BlueFerry's settings, Plugins > WebDAV files > Settings, or:

```sh
blueferry plugins config io.weirdware.blueferry.webdav \
    --set url=https://cloud.example.org/remote.php/dav/files/USER/ \
    --set username=USER --secret password
```

| Setting | Meaning |
| --- | --- |
| `url` | The WebDAV address (see below). `https://` only; plain `http://` for localhost, or for the LAN when `allow_http_lan` is on. |
| `username` | Your login name. |
| `password` | Password or app password. Checked against the server before it is stored. |
| `folder` | Target folder below the address, default `BlueFerry`; created if missing. Sub folders with `/`. |
| `public_link` | Nextcloud only: create a read-only public link after an upload. |
| `max_size_mb` | Largest file to upload or open, default 2048 MB. |
| `allow_http_lan` | Allow `http://` to a private address or a `.local`/`.lan` name. The password then travels unencrypted. |
| `web_url` | Optional page that "Ordner öffnen" opens on servers other than Nextcloud. |

### Nextcloud

- Address: `https://cloud.example.org/remote.php/dav/files/USER/` (Files >
  Files settings > WebDAV shows it). The plugin recognises this layout and
  confirms it with `status.php`; only then are chunked uploads and public
  links used.
- Use an app password (Settings > Security > Devices & sessions), not your
  login password, especially with two-factor authentication.
- Public links use the OCS sharing API with read-only permission. If
  sharing by link is disabled on the server, the upload still succeeds and
  the notification says so.

### SFTPGo

- Address: the WebDAV endpoint, for example `https://dav.example.org/`.
  Enable WebDAV for the user (and for the SFTPGo instance).
- Set `web_url` to the web client (for example
  `https://files.example.org/web/client/files`) so "Ordner öffnen" leads
  somewhere useful.
- With single sign-on in front of the web client, WebDAV still needs a
  local password or an app-specific one.

### Other WebDAV servers

Any server with `PROPFIND`, `MKCOL` and `PUT` works (Apache `mod_dav`,
nginx-dav-ext, rclone serve webdav, ownCloud, ...). Uploads go in one
stream; "Ordner öffnen" opens the WebDAV folder URL unless `web_url` is set.

## Security

- Only `https://`. Redirects are refused (they would carry the password
  elsewhere); every request has a timeout; requests only go to the
  configured server. With `allow_http_lan`, the name is resolved once per
  operation, every address must be private, and all requests of that
  operation connect to the checked address with the original `Host` header,
  so a DNS answer that changes in between (rebinding) is never followed.
- Local file names are reduced to one safe path segment and
  percent-encoded; no `..`, no separators, no control characters. Existing
  files are never overwritten: a second `report.pdf` becomes
  `report (2).pdf`.
- The password is kept in the desktop keyring (Secret Service), or in an
  owner-only file when there is none. It never appears in logs, D-Bus
  replies, the manifest or command lines; the settings form only shows
  whether one is stored. Logs contain no file contents or names, only
  counts and short error codes.
- Opened files go to `~/.cache/blueferry/webdav` (owner-only, 1 GB, least
  recently used first). Launchers and scripts (`.desktop`, `.sh`, `.exe`,
  ...) are not opened.

`blueferry-webdav forget` removes the settings and the password;
`blueferry-webdav status` shows the configured server.

## Develop

```sh
python3 -m venv --system-site-packages .venv   # dbus-python, PyGObject, libsecret from the system
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

`blueferry-plugin-api` comes from the `plugin-api` directory of the
BlueFerry repository. The tests run a real WebDAV server (wsgidav) and a
fake Nextcloud (chunked upload v2, OCS shares) on localhost, and drive the
plugin through a fake BlueFerry core that checks every reply against the
1.2 surface spec. The plugin has not been tested against a live Nextcloud
or SFTPGo yet.

## License

GPL-2.0-or-later, like BlueFerry. See `LICENSE`.
