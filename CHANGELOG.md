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
  Test build 0.2.17 on the branch `dev`; not released on `main` yet.
  New menu/settings action "Check for updates" (Kodi refreshes its repositories at once).
  New action "Write test (diagnosis)": tries several file names and write modes in the storage
  location and shows what Kodi answers.
  "Test NAS connection" now writes a test file in every folder (a folder created by another
  device can be read-only for this one); a refused write points to the folder permissions.
  Files are read in chunks until the end instead of trusting the size the share reports (Android
  NFS returned empty files), and a file that looks empty is retried. A nonsense NAS file time
  (seen: 3.6e18) no longer triggers the clock warning.
  Media are matched automatically across devices: by fingerprint, otherwise by file name and last
  folders (protocol, host, share, drive and letter case ignored). Path mappings are optional now.
  "Check for updates" now reads the repositories itself and shows the installed and the offered
  version before it asks Kodi to refresh.
  Files outside the library: a foreign full path is no longer written to Kodi as if it were this
  device's path; that only looked like success and stopped the fallback seek.
  No progress or reset event is written while the position is below 5 s: a quick look at a file
  (or a start without the saved position) overwrote the state of the medium with 0.
  Flags for files outside the library: the path this device uses is learned from its own
  playbacks (same location, or the difference between two locations) and the state is written
  with `Files.SetFileDetails`; the fallback seek stays as a safety net.
  "Playback started elsewhere" now defaults to "Ask first" (was: jump automatically). The log says
  which path was written for a file outside the library and what Kodi answered.
  Stopping right after a deliberate start from the beginning (a saved position existed) clears the
  saved position again (`reset`). Manual watched/unwatched marks on files outside the library are
  taken from the notification if it names the file; ignored notifications are logged in debug mode.
  The add-on no longer jumps to a position or asks where to continue (setting "Playback started
  elsewhere" removed): Kodi's own resume dialog is the only question. Clearing the saved position:
  a stop within 1 s (was 5 s) of the start of a medium with a saved position.
  Files outside the library: the playcount of the files this device knows is read at every sync, and a
  change since the last look (marked watched or unwatched in a file list) becomes an event, because
  Kodi does not have to announce such a change.
  A quick stop clears a saved position of any size (was: more than 5 s).
  (0.2.16 used the early start notice onPlayBackStarted; removed again in 0.2.17 after a Kodi freeze.)
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
