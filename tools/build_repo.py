#!/usr/bin/env python3
"""Build the installable Kodi repository in repo/ from addons/.

Result:
    repo/zips/<id>/<id>-<version>.zip   (folder <id>/ as the ZIP root)
    repo/zips/<id>/icon.png, fanart.jpg (if present)
    repo/addons.xml
    repo/addons.xml.md5

Old ZIP versions are kept (rollback); ZIPs of the current version are rebuilt.
Standard library only.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDONS_DIR = ROOT / "addons"
REPO_DIR = ROOT / "repo"
ZIPS_DIR = REPO_DIR / "zips"

EXCLUDE_DIRS = {"__pycache__", ".git", ".vscode", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
ASSET_FILES = ("icon.png", "fanart.jpg")
# Fixed timestamp in the ZIP: same content gives the same ZIP (no Git noise on rebuild)
ZIP_TIME = (2000, 1, 1, 0, 0, 0)


@dataclass
class Built:
    addon_id: str
    version: str
    zip_path: Path
    xml_text: str


def read_addon_xml(addon_dir: Path) -> tuple[str, str, str]:
    """Read id, version and the XML text (without declaration, LF line endings)."""
    xml_path = addon_dir / "addon.xml"
    root = ET.parse(xml_path).getroot()
    addon_id, version = root.get("id"), root.get("version")
    if not addon_id or not version:
        raise ValueError(f"{xml_path}: id or version missing")
    if addon_id != addon_dir.name:
        raise ValueError(f"Folder '{addon_dir.name}' does not match add-on id '{addon_id}'")
    text = xml_path.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    if text.startswith("<?xml"):
        text = text.split("?>", 1)[1].strip()
    return addon_id, version, text


def included_files(addon_dir: Path):
    for path in sorted(addon_dir.rglob("*")):
        rel = path.relative_to(addon_dir)
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        yield path, rel


def build_zip(addon_dir: Path, addon_id: str, version: str) -> Path:
    out_dir = ZIPS_DIR / addon_id
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{addon_id}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in included_files(addon_dir):
            info = zipfile.ZipInfo((Path(addon_id) / rel).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    for name in ASSET_FILES:
        if (addon_dir / name).is_file():
            shutil.copyfile(addon_dir / name, out_dir / name)
    return zip_path


def render_addons_xml(entries: list[str]) -> bytes:
    text = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
        + "\n".join(entries)
        + "\n</addons>\n"
    )
    ET.fromstring(text.split("?>", 1)[1])  # must be valid XML
    return text.encode("utf-8")


def find_addon_dirs() -> list[Path]:
    return [d for d in sorted(ADDONS_DIR.iterdir()) if (d / "addon.xml").is_file()]


def human_size(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"


def main() -> int:
    built: list[Built] = []
    for addon_dir in find_addon_dirs():
        try:
            addon_id, version, text = read_addon_xml(addon_dir)
        except (ValueError, ET.ParseError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        built.append(Built(addon_id, version, build_zip(addon_dir, addon_id, version), text))

    if not built:
        print("ERROR: no add-ons found in addons/", file=sys.stderr)
        return 1

    data = render_addons_xml([b.xml_text for b in built])
    REPO_DIR.mkdir(exist_ok=True)
    (REPO_DIR / "addons.xml").write_bytes(data)
    (REPO_DIR / "addons.xml.md5").write_text(hashlib.md5(data).hexdigest(), encoding="ascii")

    print(f"{'Addon':<36}{'Version':<10}{'ZIP size':>12}")
    print("-" * 58)
    for b in built:
        print(f"{b.addon_id:<36}{b.version:<10}{human_size(b.zip_path.stat().st_size):>12}")
    print("-" * 58)
    print(f"{len(built)} Addon(s) -> repo/addons.xml, repo/addons.xml.md5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
