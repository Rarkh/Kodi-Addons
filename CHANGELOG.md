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
- Repository layout: `addons/` (sources), `repo/` (generated), `tools/build_repo.py`, VS Code
  build task and an optional GitHub Action that rebuilds `repo/` on push.
- `repository.rarkh` 1.0.0: repository add-on that serves `repo/` from GitHub (raw).
- `plugin.program.artlibcleanup` 0.1.0: finds and removes cached images (fanart, screenshots,
  actor thumbnails) of items that are no longer in the video or music library. Preview (dry run)
  by default; German and English texts.

### Changed
- `service.watchsync` moved from the repository root to `addons/` (version unchanged, 0.1.0).

### Removed
- `repository.meinrepo` (GitHub Pages), the old `build.py` and `zips/`, replaced by the layout above.
