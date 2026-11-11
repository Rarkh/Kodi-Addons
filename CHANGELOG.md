# Changelog

All notable changes are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Version numbers

Every add-on in this repository has its own version and uses
[Semantic Versioning](https://semver.org/): **MAJOR.MINOR.PATCH**, for example `0.1.0`.

| Part  | Raised when ...                                             | Example        |
|-------|-------------------------------------------------------------|----------------|
| MAJOR | something changes in a way that breaks existing use         | 0.4.1 -> 1.0.0 |
| MINOR | a feature is added, existing behaviour keeps working        | 0.4.1 -> 0.5.0 |
| PATCH | only bugs are fixed, nothing new                            | 0.4.0 -> 0.4.1 |

- Raising a part resets the parts to its right to 0.
- **0.y.z** means the add-on is still in development; a MINOR release may still change things.
- The version of an add-on is set once, in the `version` attribute of its `addons/<id>/addon.xml`
  (Kodi updates an add-on only if that number is higher than the installed one).
- Releases are tagged in Git as `<addon-id>/vMAJOR.MINOR.PATCH`, for example
  `plugin.program.artlibcleanup/v0.1.0`.
- Changes not yet released are collected under `[Unreleased]`. Entries name the add-on.

## [Unreleased]
### Fixed
- `service.watchsync` 0.3.1 (test build): a device could not take over the state of files that another
  device plays from a location it cannot translate (e.g. a tablet's local `/storage/...` path seen by
  a phone that only plays from the NAS): nothing was written and the entry was never tried again.
  Files seen in a shown folder now teach the path (one per location), and entries without a
  local path are tried again after that.

### Added
- `repository.rarkh.test` 1.0.0: repository add-on of a test channel (branch `dev`, folder
  `repo-test/`). `tools/build_repo.py --channel test --only <id>` builds it with just the named
  add-ons, so no add-on id is offered by both channels; the release channel leaves it out.

### Changed
- `tools/build_repo.py`: the MD5 checksum is marked as not security-relevant (Kodi needs MD5).

## [service.watchsync 0.3.0]
### Added
- First release. Keeps watch status (watched/unwatched, resume point) in sync between several Kodi
  installations through small files on a NAS. Only the Kodi API and the Python standard library;
  nothing runs on the NAS.
- Storage as immutable event files and per-device snapshots (written via `xbmcvfs` as tmp + rename,
  no locking), merged by the newest UTC timestamp; offline queue, local cache, each device cleans up
  only its own events.
- Media are recognised automatically across devices (by fingerprint, otherwise by file name and
  last folders; protocol, host, share, drive and letter case do not matter). Path mappings are
  optional.
- The synced state is written to Kodi's database (library items and files outside the library), so
  Kodi's own "resume from ..." dialog asks where to continue. The add-on never jumps to a position
  and never asks by itself.
- Marks set by hand are sent: library items through Kodi's notification, files outside the
  library by looking at the folder shown in the file view when a mark was set (also whole
  folders). Stopping within 1 s of the start clears a saved position on all devices.
- Program menu and settings: sync now, status, path mappings, log, rebuild cache, connection test
  (writes in every folder), write test, check for updates (shows installed and offered version).
- Made for big libraries: incremental merge, only changed entries go to Kodi, split cache.
  Measured with 10,000 media: a sync round without news takes well under a millisecond.
- English and German texts, README with architecture, settings and a two-device test guide.
- NAS permissions: all devices must be allowed to write into the sub folders (see the README).

## [plugin.program.artlibcleanup 0.1.4]
### Changed
- Internal refactoring for SonarQube (smaller functions, shared constants, one constant for the
  `Textures.GetTextures` method name), no change in behaviour.

## [plugin.program.artlibcleanup 0.1.3]
### Fixed
- Sizes showed as 0 KB: the `size` in the texture database is a size class, not bytes. The size is now read from the cached file (`special://thumbnails/`).

## [repository.rarkh 1.0.2]
### Changed
- New icon, cut from the fanart (the old one was a placeholder).

## [repository.rarkh 1.0.1]
### Changed
- New fanart image.

## [plugin.program.artlibcleanup 0.1.2]
### Changed
- New fanart image.
- Test suite covers 100 % of the add-on code and `tools/` (lines and branches); the pipeline fails
  below 100 %.

## [plugin.program.artlibcleanup 0.1.1]
### Fixed
- `Textures.GetTextures` failed with "Invalid params": the image cache is now read in one call without `limits`.
### Added
- Repository layout: `addons/` (sources), `repo/` (generated), `tools/build_repo.py`, VS Code
  build task.
- `repository.rarkh` 1.0.0: repository add-on that serves `repo/` from GitHub (raw).
- `plugin.program.artlibcleanup` 0.1.0: finds and removes cached images (fanart, screenshots,
  actor thumbnails) of items that are no longer in the video or music library. Preview (dry run)
  by default; German and English texts.

### Changed
- `service.watchsync` moved from the repository root to `addons/` (version unchanged, 0.1.0).

### Removed
- `repository.meinrepo` (GitHub Pages), the old `build.py` and `zips/`, replaced by the layout above.
