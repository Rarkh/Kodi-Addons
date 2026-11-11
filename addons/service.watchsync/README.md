# WatchSync

Kodi 21 (Omega) add-on that keeps watch status (watched/unwatched, resume point) in sync between
several Kodi installations (Android, Windows, Linux, CoreELEC, LibreELEC). The status lives as
small files on a NAS share. Nothing runs on the NAS; only the Kodi Python API and the Python
standard library are used.

- Media are recognised by their **path on the NAS**, never by Kodi database ids.
- Files that are **not in the Kodi library** are tracked too (played from an NFS/SMB source).
- Status 0.1: development version, see "What still has to be tested on real devices".

## Architecture: immutable event files

```
WatchSync/events/<device>_<seq 6 digits>.json    one file = one event, written once
WatchSync/devices/<device>.json                  name, last contact, last sequence number
WatchSync/snapshots/<device>.json                condensed own state
```

There is no file locking on a NAS, so there is nothing to lock:

1. A device only writes its own files and never overwrites a foreign file.
2. Writing: `<name>.tmp`, read back and compare, then rename to `<name>.json`. Readers ignore
   `.tmp` files and files they cannot parse yet (retry at the next round, after 5 failed
   attempts the file is marked defective and logged).
3. Every device remembers per foreign device the highest sequence number it has processed.
4. Conflicts: per medium the event with the **newest UTC timestamp** wins (not the largest
   position, so rewinding on purpose works). Equal timestamps: the alphabetically larger device
   id wins. Events more than 24 h in the future are ignored and logged.
5. Clean-up: a device condenses and deletes only its **own** old events, after it has written
   its snapshot (from 100 own event files on).
6. At start the add-on compares the device clock with the file time of the NAS
   (`xbmcvfs.Stat`) and logs a warning if they differ by more than 2 minutes.
7. Progress events go to the NAS at most once per 60 s per medium; in between the position is
   only saved locally. Pause, stop and end always write. Local-only progress left over by a
   crash is sent at the next start.

Event format (version 1, `type` = `progress | watched | unwatched | reset`):

```json
{"v": 1, "device": "android-wohnzimmer", "seq": 42, "ts": "2026-11-10T11:47:03.512Z",
 "type": "progress", "media": {"id": "Filme/Alien.mkv", "fp": "s=4812345678;h=9f2c...;"},
 "position_s": 3120.5, "duration_s": 7020.0, "playcount": 0}
```

Merging groups events per medium: same `media.id`, or the same fingerprint under different ids
(moved/renamed file). A device never reuses a sequence number; if the local cache is lost it
continues after everything of its own it finds on the NAS.

### Modules (`watchsync/`)

| Module       | Task                                                                    |
|--------------|-------------------------------------------------------------------------|
| `mediaid`    | path mappings, media id, fingerprint (size + SHA-1 of first/last 64 KB) |
| `events`     | event and snapshot format, strict parsing                               |
| `merge`      | merge and conflict rules                                                |
| `backend`    | storage interface and the `xbmcvfs` backend (replaceable, e.g. HTTP)    |
| `store`      | local cache in the profile (counter, queue, processed events, applied)  |
| `engine`     | queue/upload, reading, snapshots, clean-up, recovery                    |
| `applier`    | writes the NAS state into Kodi's database, notices manual changes       |
| `player`     | `xbmc.Player` adapter and the playback tracker                          |
| `runtime`    | the service loop; `menu` is the program add-on entry                    |

## Media identification (automatic) and path mappings (optional)

No setup is needed. The same file looks different on every device (`nfs://192.168.178.57/RAID5/Filme/Alien.mkv`,
`smb://nas/RAID5/Filme/Alien.mkv`, `R:\Filme\Alien.mkv`, `/media/nas/Filme/Alien.mkv`), so a
medium is recognised in three steps:

1. **Fingerprint** (size plus SHA-1 of the first and last 64 KB, taken when a playback starts):
   events with the same fingerprint are the same medium, even under another path or name.
2. **File name and last folders:** protocol, host, share and drive letter are ignored, the
   comparison is case-insensitive, and the last up to three parts (`Show/Season 01/E01.mkv`) must
   agree. The file name alone never matches; at least the file name and its folder are needed.
   This is how a library item on this device is found for a state written by another device.
3. **Path mapping (optional):** if a share is laid out differently on two devices so that step 2
   cannot work, list the beginnings of the paths this device sees; the longest prefix wins:

```
nfs://192.168.178.57/RAID5/        on the Android box
R:\                                on Windows
nfs://192.168.178.57/RAID5/Serien/=>Series      optional: "prefix=>base" puts a base in front
```

Mapped paths turn into the same id (`Filme/Alien.mkv`) on every device. Entries are separated by
`|` in the setting (or edit them in the menu *Edit path mappings*). Credentials
(`smb://user:pw@`) are never stored and URL escapes are decoded. Without a mapping the event
carries the full path as id and `"unmapped": true`; this is normal and works through steps 1 and 2.

Limit of step 2: when a share has the same folders but another share name on two devices **and**
the file lies only one folder below the share, the share name counts as a third part and the
files do not match (the fingerprint still does once both devices played the file).

## What happens on a device

- **Playback:** `onAVStarted`, pause, stop and end create events (also for files that are not in
  the library). The position is saved locally every N seconds (default 10), at pause/stop/end an
  event is written. From 90 % (setting) or at the end the medium counts as watched
  (`playcount` + 1); no progress is written while the position is below 1 s, so a quick look at a file never overwrites the position another playback saved. Stopping within 1 s of the start of a medium that had a saved position clears that position on all devices (`reset`), also after the automatic jump to it.
- **Kodi start and every sync interval (default 30 s):** the NAS state is read and merged.

### Reconciling with Kodi's own database

The NAS is the truth; Kodi's database is only the display on this device.

1. The profile keeps a list "last set by me in Kodi" (id, playcount, position, timestamp).
   An entry is written to Kodi only if its timestamp is **newer** than that list says.
2. **Echo suppression:** before writing playcount/resume by JSON-RPC the value is remembered.
   A `VideoLibrary.OnUpdate` with exactly this playcount within 5 s is ignored and creates no event.
3. **Manual changes:** marking a library item watched/unwatched in Kodi arrives as
   `VideoLibrary.OnUpdate`; the file is resolved with `Get...Details` and a `watched` or
   `unwatched` event is created (not if the state on the NAS already has this playcount, e.g. the
   value Kodi writes itself at the end of a playback).
4. **Resume point in advance:** the NAS state is applied to Kodi's database at start and at
   every sync, so Kodi's own "resume or start from beginning" works. `seekTime()` after
   `onAVStarted` is only the fallback (file without library entry, or applying did not work).
   Setting *Playback started elsewhere* (jump automatically / ask first) controls that fallback.
5. Positions Kodi writes itself at stop are harmless: the add-on writes its own event with the
   same value.
6. While a medium is **playing**, foreign events for it are not applied to Kodi; they wait and are
   compared with the own stop event by the merge rules afterwards.
7. **Other sync add-ons (e.g. Trakt)** change the same fields. Disable them on devices that run
   WatchSync.

**Flags for files outside the library** (watched mark, "in progress" mark in file lists): Kodi keeps
them per path, so the add-on needs the path *this* device uses. It learns it from the files this
device played itself: if another device has an event for the same file under another location
(`smb://nas/` there, `nfs://192.168.0.5/` here), the difference is the translation for all other
files of that share; a location this device also uses needs none. Without a known path nothing is
written (the fallback seek at the start of the playback still works). Written with
`Files.SetFileDetails`, which has to be tested on real devices.

Not in Phase 1: repository set-up, Docker/HTTP backend, series special cases beyond the path id.

## Settings

| Setting                    | Default | Meaning                                                      |
|----------------------------|---------|--------------------------------------------------------------|
| Storage location           | -       | NAS folder, e.g. `nfs://192.168.178.57/RAID5/KODI-Files/WatchSync/` |
| Device name                | Kodi name | unique id (a-z, 0-9, `-`); derived from the Kodi name if empty |
| Path mappings              | -       | see above                                                    |
| Check for new events every | 30 s    | sync interval                                                |
| Save position every        | 10 s    | local position saving while playing                          |
| Mark as watched from       | 90 %    |                                                              |
| Playback started elsewhere | automatic | fallback seek: automatically or ask first                  |
| Use local cache            | on      | off: queue and state are not kept between restarts           |
| Log level                  | normal  | debug adds details to the Kodi log                           |

Menu (*Programs > WatchSync*, also buttons in the settings): sync now, show status, edit path
mappings, show log, rebuild cache (read everything from the NAS again, unsent events are kept),
test NAS connection, check for updates (shows the installed and the offered version, and makes Kodi refresh its repositories), write test (tries several ways of writing a file to the storage location and shows what Kodi answers). Menu and service talk through notifications and files in the profile; the
service is the only one that writes events.

## NAS permissions (important with several devices)

Every device creates the folders `events/`, `devices/` and `snapshots/` itself if they are missing,
and the NAS gives them to the user the NFS access is mapped to (often mode 755). A second device
with a different NFS user then **cannot write** into them, although it can read and create files in
the parent folder. The symptom is `cannot write ...: the write was refused`; *Test NAS connection*
checks every folder and names the one that fails.

Fix it once on the NAS, one of:

- NFS export: map all users to one user (all_squash with a fixed anonuid/anongid) and give that
  user write access.
- Give the storage folder and its sub folders full access for everybody (mode 777, on Windows
  `icacls <folder> /grant Jeder:(OI)(CI)F /T` for a share that maps this to unix rights).
- Create the three folders once by hand (e.g. in Explorer on the share) so that they inherit the
  rights of the parent folder.

## Tests

`python -m pytest` (unit tests with a fake `xbmcvfs`, 100 % coverage). They cover media ids,
format, merge and conflict rules, snapshots, tmp+rename, fault tolerance, offline queue, echo
suppression, "NAS newer than applied" and "defer while playing" with mock timestamps.

### Trying it with two real Kodi devices

1. Install the add-on on both, give each a different device name and the **same** storage
   location; *Test NAS connection* must succeed on both. Path mappings are not needed.
2. Play a movie from the share on device A for a minute, pause. On B: within 30 s the resume
   point appears (library item) or the fallback seek happens when starting the file.
3. Mark something watched on B; A shows it after the next sync. Mark it unwatched on A again.
4. Rewind on purpose on B (stop at 0:30 after A was at 20:00): A must follow, not keep 20:00.
5. Pull the network cable while playing, stop, reconnect: the status shows the queued events,
   they are sent. Look at `events/` on the NAS: only `.json` files and no `.tmp` leftovers.
6. Check the clock warning: set one device's time wrong, see the log entry.

## What still has to be tested on real devices

Everything was tested with a fake `xbmcvfs` and fake JSON-RPC only. In particular:

- **`Files.SetFileDetails`** for files outside the library on Kodi 21 (used to apply watched
  status and resume to such files). If it fails it is logged once, the fallback seek remains,
  but the "watched" mark of those files will not show in Kodi's file lists.
- **NFS behaviour on Android**: `xbmcvfs` rename over an existing file, `listdir` on large
  folders, attribute caching (a new event may show up a few seconds late), `Stat.st_mtime`.
- Whether `getTime()` is reliable in `onPlayBackPaused`, and the timing of Kodi's own resume
  dialog compared with the pre-applied resume point.
- Notification payloads of `VideoLibrary.OnUpdate` for episodes and music videos.
- Settings dialog: `path` setting browsing NFS shares, the action buttons.
