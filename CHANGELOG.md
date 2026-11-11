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
### Added
- `repository.rarkh.test` 1.0.0: repository add-on of a test channel (branch `dev`, folder
  `repo-test/`). `tools/build_repo.py --channel test --only <id>` builds it with just the named
  add-ons, so no add-on id is offered by both channels; the release channel leaves it out.
- `service.watchsync`: first working version (Phase 1). Watch status is stored as immutable
  event files and snapshots on a NAS (via `xbmcvfs`, tmp + rename, no locking), merged by newest
  UTC timestamp, applied to Kodi's database only when newer than what was applied before, with
  echo suppression, capture of manual watched/unwatched marks, resume point applied in advance
  and a fallback seek. Settings, program menu, path mappings, offline queue, local cache.
  Test build 0.2.1 on the branch `dev`; not released on `main` yet.
  Writing waits for NFS attributes to settle before it counts a read-back as failed, and the
  error says whether the write was refused or what was read back.

### Changed
- `plugin.program.artlibcleanup`: internal refactoring for SonarQube (smaller functions, shared
  constants), no change in behaviour.
- `tools/build_repo.py`: the MD5 checksum is marked as not security-relevant (Kodi needs MD5).

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
