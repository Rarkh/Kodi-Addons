# Kodi-Addons

Mono-repo for my Kodi add-ons. `tools/build_repo.py` builds an installable Kodi repository in
`repo/` from `addons/`; Kodi loads it straight from GitHub (raw).

```
addons/                  source code, one folder per add-on
  repository.rarkh/      the repository add-on (install once in Kodi)
  repository.rarkh.test/ repository add-on of the test channel (test devices only)
  plugin.program.artlibcleanup/
  service.watchsync/
repo/                    generated and checked in: addons.xml, addons.xml.md5, zips/
repo-test/               same for the test channel (only on the branch "dev")
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
   <https://raw.githubusercontent.com/Rarkh/Kodi-Addons/main/repo/zips/repository.rarkh/repository.rarkh-1.0.2.zip>
2. In Kodi: *Settings → Add-ons → Install from zip file* (allow "Unknown sources" if asked)
   and pick the ZIP.
3. Then: *Add-ons → Install from repository → Rarkh Repository* and install the add-ons you
   want. Updates arrive automatically.

## Test channel (test deploys without touching releases)

A second repository add-on, `repository.rarkh.test`, points to the branch `dev` and the folder
`repo-test/`. Test devices get pre-releases from there; the release repository on `main` stays
untouched.

One-time setup per test device:

1. Download the ZIP (or put it on the NAS and install from there):
   <https://raw.githubusercontent.com/Rarkh/Kodi-Addons/dev/repo-test/zips/repository.rarkh.test/repository.rarkh.test-1.0.0-rc1.zip> (the number after `-rc` is the newest build; see the folder `repo-test/zips/repository.rarkh.test/`)
2. *Settings → Add-ons → Install from zip file* and pick it.
3. *Install from repository → Rarkh Repository (Test)* and install the add-on. Updates follow
   automatically.

The test channel contains only the add-ons you name with `--only` (plus the test repository
itself), so no add-on id is offered by both channels. Both repositories can be installed on one
device; keep an add-on that is under test out of `repository.rarkh` until it is released.

Everything in the test channel is marked as a test: the build adds `-rcN` to the version
(`0.3.4-rc1`, `0.3.4-rc2`, ... counting up, because Kodi only updates to a higher version), the
name gets "(Test)" and the icon a red TEST band (made with Pillow; an add-on without an icon gets one
made from its name). `addons/` stays clean: `addon.xml` holds the number of the next release.
Kodi ranks `0.3.4-rc2` above `0.3.4`, so a device on a test build is offered the release only
with the next number (`0.3.5`) or after a reinstall.

Test deploy (the branch `dev` must be created once with `git switch -c dev`):

```
git switch dev
# raise the version in addons/<id>/addon.xml (Kodi updates only to a higher version)
python tools/build_repo.py --channel test --only service.watchsync
git add -A
git commit -m "Test build <addon> <version>"
git push origin dev
```

Release: merge `dev` into `main`, then build the release channel (`python tools/build_repo.py`).
`repo-test/` belongs to `dev`; do not merge it into `main` (or delete it before the release).

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
