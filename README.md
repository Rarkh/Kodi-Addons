# Kodi-Addons

Mono-repo for my Kodi add-ons. `tools/build_repo.py` builds an installable Kodi repository in
`repo/` from `addons/`; Kodi loads it straight from GitHub (raw).

```
addons/                  source code, one folder per add-on
  repository.rarkh/      the repository add-on (install once in Kodi)
  plugin.program.artlibcleanup/
  service.watchsync/
repo/                    generated and checked in: addons.xml, addons.xml.md5, zips/
tools/build_repo.py      builds ZIPs, addons.xml and the checksum
tests/                   unit tests
```

## One-time setup

```
pip install -r requirements-dev.txt
```

`Kodistubs` provides autocompletion for `xbmc`, `xbmcgui` etc. in VS Code.

## Create a new add-on

1. Create `addons/<addon-id>/` with an `addon.xml`. The id must equal the folder name
   (`plugin.program.xyz`, `service.xyz`, `script.xyz` ...).
2. Optionally put an `icon.png` (e.g. 512x512) and `fanart.jpg` (1920x1080) next to it.
3. Build (see below). The add-on then appears in the repository.

## Raise a version

Increase the `version` attribute in `addons/<addon-id>/addon.xml` (MAJOR.MINOR.PATCH) and add
an entry to `CHANGELOG.md`. Kodi only updates an add-on if the version in `repo/addons.xml` is
higher than the installed one. No new version, no update.

## Build

In VS Code press **Ctrl+Shift+B** (task "Build Repo") or run:

```
python tools/build_repo.py
```

For every add-on this creates `repo/zips/<id>/<id>-<version>.zip`, copies icon and fanart,
writes `repo/addons.xml` and `repo/addons.xml.md5` and prints a summary. Old ZIP versions stay
in place (rollback possible); ZIPs of the current version are rebuilt.

## Push

```
git add -A
git commit -m "Update <addon> to <version>"
git push
```

`repo/` must be committed, because Kodi reads exactly these files. Always build locally before committing. The GitHub repository must
be **public**.

## Install the repository in Kodi

1. Download the ZIP:
   <https://raw.githubusercontent.com/Rarkh/Kodi-Addons/main/repo/zips/repository.rarkh/repository.rarkh-1.0.1.zip>
2. In Kodi: *Settings → Add-ons → Install from zip file* (allow "Unknown sources" if asked)
   and pick the ZIP.
3. Then: *Add-ons → Install from repository → Rarkh Repository* and install the add-ons you
   want. Updates arrive automatically.

## Development tests with a junction

To let Kodi use the source code directly (no ZIP install), link the add-on folder
(Command Prompt, not PowerShell):

```
mklink /J "%APPDATA%\Kodi\addons\<id>" "C:\CLAUDE\Kodi-Addons\addons\<id>"
```

Restart Kodi afterwards (new add-ons are detected at startup). Changes to `.py` files apply the
next time the add-on is run; services need a restart of the add-on or of Kodi. Remove the
junction with `rmdir "%APPDATA%\Kodi\addons\<id>"` (this deletes only the link, not the source).
Do not keep the same id installed from the repository at the same time.
