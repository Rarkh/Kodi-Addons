# Kodi-Addons

Mono-Repo für meine Kodi-Addons. Aus `addons/` baut `tools/build_repo.py` ein installierbares
Kodi-Repository in `repo/`, das direkt von GitHub (raw) geladen wird.

```
addons/                  Quellcode, ein Ordner pro Addon
  repository.alex/       das Repository-Addon (einmal in Kodi installieren)
  plugin.program.artlibcleanup/
  service.watchsync/
repo/                    generiert und eingecheckt: addons.xml, addons.xml.md5, zips/
tools/build_repo.py      baut ZIPs, addons.xml und Prüfsumme
tests/                   Unit-Tests
```

## Einmalige Einrichtung

```
pip install -r requirements-dev.txt
```

`Kodistubs` liefert die Autovervollständigung für `xbmc`, `xbmcgui` usw. in VS Code.

## Neues Addon anlegen

1. Ordner `addons/<addon-id>/` mit `addon.xml` anlegen. Die ID muss dem Ordnernamen entsprechen
   (`plugin.program.xyz`, `service.xyz`, `script.xyz` ...).
2. Optional `icon.png` (z. B. 512x512) und `fanart.jpg` (1920x1080) daneben legen.
3. Bauen (siehe unten). Das Addon erscheint danach im Repository.

## Version erhöhen

In `addons/<addon-id>/addon.xml` das Attribut `version` hochzählen (MAJOR.MINOR.PATCH) und
den Eintrag in `CHANGELOG.md` ergänzen. Kodi aktualisiert ein Addon nur, wenn die Version in
`repo/addons.xml` höher ist als die installierte. Ohne neue Version kein Update.

## Bauen

In VS Code **Strg+Shift+B** (Task "Build Repo") oder:

```
python tools/build_repo.py
```

Das Skript erzeugt pro Addon `repo/zips/<id>/<id>-<version>.zip`, kopiert Icon und Fanart,
schreibt `repo/addons.xml` und `repo/addons.xml.md5` und gibt eine Übersicht aus. Alte
ZIP-Versionen bleiben liegen (Rollback möglich), ZIPs der aktuellen Version werden neu gebaut.

## Pushen

```
git add -A
git commit -m "Update <addon> to <version>"
git push
```

`repo/` muss mit eingecheckt werden, Kodi liest genau diese Dateien. Das GitHub-Repo muss
**öffentlich** sein.

## Repository in Kodi installieren

1. ZIP herunterladen:
   <https://raw.githubusercontent.com/Rarkh/Kodi-Addons/main/repo/zips/repository.alex/repository.alex-1.0.0.zip>
2. Kodi: *Einstellungen → Addons → Aus ZIP-Datei installieren* (ggf. "Unbekannte Quellen"
   erlauben) und die ZIP wählen.
3. Danach: *Addons → Aus Repository installieren → Alex Repository* und die gewünschten
   Addons installieren. Updates kommen automatisch.

## Entwicklungstests per Junction

Damit Kodi direkt den Quellcode benutzt (ohne ZIP-Installation), den Addon-Ordner verlinken
(Eingabeaufforderung, nicht PowerShell):

```
mklink /J "%APPDATA%\Kodi\addons\<id>" "C:\CLAUDE\Kodi-Addons\addons\<id>"
```

Kodi danach neu starten (neue Addons werden beim Start erkannt). Änderungen an `.py`-Dateien
gelten beim nächsten Aufruf des Addons; Services brauchen einen Neustart des Addons oder von
Kodi. Die Junction mit `rmdir "%APPDATA%\Kodi\addons\<id>"` wieder entfernen (löscht nur den
Link, nicht den Quellcode). Nicht gleichzeitig dieselbe ID aus dem Repository installiert
lassen.

## Automatischer Build bei Push (optional)

`.github/workflows/build.yml` führt bei jedem Push auf `main`, der `addons/` oder `tools/`
ändert, `tools/build_repo.py` aus und committet `repo/`, falls sich etwas geändert hat.

Überschneidung mit lokalem Bauen: Beides erzeugt dieselben Dateien. Die ZIPs sind
reproduzierbar (feste Zeitstempel), daher ändert der Server-Build nichts, wenn du lokal
schon gebaut und mitgepusht hast. Baust du nicht lokal, erzeugt die Action einen zusätzlichen
Commit; vor dem nächsten Push dann `git pull`. Wer immer lokal baut, kann die Datei löschen.
