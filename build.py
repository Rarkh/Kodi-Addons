#!/usr/bin/env python3
"""Baut alle Kodi-Addons zu ZIPs und erzeugt addons.xml sowie addons.xml.md5.

Jeder Unterordner mit einer addon.xml gilt als Addon. Ergebnis:
    zips/<id>/<id>-<version>.zip
    zips/addons.xml
    zips/addons.xml.md5
"""
import hashlib
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ZIPS = ROOT / "zips"
EXCLUDE_DIRS = {".git", "__pycache__", "zips", ".github", ".idea", ".vscode"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def find_addons():
    for d in sorted(ROOT.iterdir()):
        if d.is_dir() and d.name not in EXCLUDE_DIRS and not d.name.startswith("."):
            if (d / "addon.xml").is_file():
                yield d


def included_files(addon_dir):
    for f in sorted(addon_dir.rglob("*")):
        rel = f.relative_to(addon_dir)
        if not f.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
            continue
        if f.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        yield f, rel


def build_zip(addon_dir, addon_id, version):
    out_dir = ZIPS / addon_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{addon_id}-*.zip"):
        old.unlink()
    zip_path = out_dir / f"{addon_id}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f, rel in included_files(addon_dir):
            zf.write(f, (Path(addon_id) / rel).as_posix())
    return zip_path


def main():
    ZIPS.mkdir(exist_ok=True)
    entries = []
    for addon_dir in find_addons():
        xml_path = addon_dir / "addon.xml"
        root = ET.parse(xml_path).getroot()
        addon_id, version = root.get("id"), root.get("version")
        if not addon_id or not version:
            sys.exit(f"{xml_path}: id oder version fehlt")
        if addon_id != addon_dir.name:
            print(f"Warnung: Ordner '{addon_dir.name}' != Addon-ID '{addon_id}'")
        zip_path = build_zip(addon_dir, addon_id, version)
        print(f"ZIP: {zip_path.relative_to(ROOT).as_posix()}")
        text = xml_path.read_text(encoding="utf-8").strip()
        if text.startswith("<?xml"):
            text = text.split("?>", 1)[1].strip()
        entries.append(text)

    addons_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
        + "\n".join(entries)
        + "\n</addons>\n"
    )
    data = addons_xml.encode("utf-8")
    (ZIPS / "addons.xml").write_bytes(data)
    (ZIPS / "addons.xml.md5").write_text(hashlib.md5(data).hexdigest(), encoding="ascii")
    print(f"{len(entries)} Addon(s) -> zips/addons.xml, zips/addons.xml.md5")


if __name__ == "__main__":
    main()
