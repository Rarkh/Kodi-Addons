#!/usr/bin/env python3
"""Build the installable Kodi repository in repo/ from addons/.

Usage:
    python tools/build_repo.py                 release channel -> repo/
    python tools/build_repo.py --channel test --only <id> [--only <id> ...]
                                               test channel    -> repo-test/ (branch "dev")

Result (per channel folder):
    zips/<id>/<id>-<version>.zip   (folder <id>/ as the ZIP root)
    zips/<id>/icon.png, fanart.jpg (if present)
    addons.xml
    addons.xml.md5

Packages of the test channel are marked as such: the version gets the suffix -rcN (Kodi only
updates to a higher version, so every build counts up: 0.3.4-rc1, 0.3.4-rc2, ...), the name gets
"(Test)" and the icon a red "TEST" band (needs Pillow; an add-on without an icon gets one made from
its name). The sources in addons/ stay clean: addon.xml holds the number of the next release.

The release channel leaves out the test repository add-on. The test channel contains only the
test repository add-on plus the add-ons named with --only, so no add-on id is offered by both
channels and a device can never be updated from the wrong one. It is rebuilt from scratch.

Release channel: old ZIP versions are kept (rollback); ZIPs of the current version are rebuilt.
Standard library only; the test icons need Pillow.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
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
TEST_REPO_DIR = ROOT / "repo-test"
TEST_ONLY = {"repository.rarkh.test"}

EXCLUDE_DIRS = {"__pycache__", ".git", ".vscode", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
ASSET_FILES = ("icon.png", "fanart.jpg")
TEST_NAME_SUFFIX = " (Test)"
ICON_SIZE = 512
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


def build_zip(
    addon_dir: Path,
    addon_id: str,
    version: str,
    zips_dir: Path | None = None,
    overrides: dict[str, bytes] | None = None,
) -> Path:
    """The ZIP of an add-on; overrides replace (or add) files by their path inside the add-on."""
    changed = overrides or {}
    out_dir = (zips_dir or ZIPS_DIR) / addon_id
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{addon_id}-{version}.zip"
    files = {rel.as_posix(): path.read_bytes() for path, rel in included_files(addon_dir)}
    files.update(changed)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo((Path(addon_id) / name).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    for name in ASSET_FILES:
        if name in changed:
            (out_dir / name).write_bytes(changed[name])
        elif (addon_dir / name).is_file():
            shutil.copyfile(addon_dir / name, out_dir / name)
    return zip_path


def rc_version(base: str, previous: str | None) -> str:
    """The test version of a build: base-rc1, or one more than the previous build of that base."""
    match = re.fullmatch(re.escape(base) + r"-rc(\d+)", previous or "")
    return f"{base}-rc{int(match.group(1)) + 1 if match else 1}"


def retag(text: str, version: str, name: str) -> str:
    """addon.xml text with another version and name in the <addon ...> tag; the rest stays."""
    start = text.index("<addon ")
    end = text.index(">", start)
    tag = text[start : end + 1]
    tag = re.sub(r'\bversion="[^"]*"', f'version="{version}"', tag, count=1)
    tag = re.sub(r'\bname="[^"]*"', f'name="{name}"', tag, count=1)
    return text[:start] + tag + text[end + 1 :]


def test_icon(addon_dir: Path, name: str) -> bytes:
    """The add-on's icon (or one made from its name) with a red TEST band; PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont  # only the test channel needs Pillow

    source = addon_dir / "icon.png"
    if source.is_file():
        image = Image.open(source).convert("RGBA").resize((ICON_SIZE, ICON_SIZE))
    else:
        image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (32, 52, 84, 255))
        ImageDraw.Draw(image).text(
            (ICON_SIZE // 2, ICON_SIZE // 3),
            name[: -len(TEST_NAME_SUFFIX)] if name.endswith(TEST_NAME_SUFFIX) else name,
            fill=(255, 255, 255, 255),
            font=ImageFont.load_default(ICON_SIZE // 9),
            anchor="mm",
        )
    draw = ImageDraw.Draw(image)
    top = ICON_SIZE * 2 // 3
    draw.rectangle((0, top, ICON_SIZE, top + ICON_SIZE // 4), fill=(200, 30, 30, 255))
    draw.text(
        (ICON_SIZE // 2, top + ICON_SIZE // 8),
        "TEST",
        fill=(255, 255, 255, 255),
        font=ImageFont.load_default(ICON_SIZE // 6),
        anchor="mm",
    )
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def previous_versions(repo_dir: Path) -> dict[str, str]:
    """id -> version of the add-ons listed in an existing addons.xml (empty if there is none)."""
    index = repo_dir / "addons.xml"
    if not index.is_file():
        return {}
    root = ET.fromstring(index.read_text(encoding="utf-8").split("?>", 1)[1])
    return {str(a.get("id")): str(a.get("version")) for a in root.iter("addon")}


def render_addons_xml(entries: list[str]) -> bytes:
    text = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
        + "\n".join(entries)
        + "\n</addons>\n"
    )
    ET.fromstring(text.split("?>", 1)[1])  # must be valid XML
    return text.encode("utf-8")


def mark_as_test(
    addon_dir: Path, version: str, previous: str | None
) -> tuple[str, str, dict[str, bytes]]:
    """(test version, addon.xml text for addons.xml, files to replace inside the ZIP)."""
    root = ET.parse(addon_dir / "addon.xml").getroot()
    name = str(root.get("name"))
    if not name.endswith(TEST_NAME_SUFFIX):
        name += TEST_NAME_SUFFIX
    version = rc_version(version, previous)
    raw = (addon_dir / "addon.xml").read_text(encoding="utf-8").replace("\r\n", "\n")
    full = retag(raw, version, name)
    text = full.strip()
    if text.startswith("<?xml"):
        text = text.split("?>", 1)[1].strip()
    overrides = {"addon.xml": full.encode("utf-8"), "icon.png": test_icon(addon_dir, name)}
    return version, text, overrides


def find_addon_dirs() -> list[Path]:
    return [d for d in sorted(ADDONS_DIR.iterdir()) if (d / "addon.xml").is_file()]


def human_size(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Kodi repository.")
    parser.add_argument("--channel", choices=("release", "test"), default="release")
    parser.add_argument("--only", action="append", default=[], metavar="ID")
    args = parser.parse_args(argv)
    test = args.channel == "test"
    repo_dir = TEST_REPO_DIR if test else REPO_DIR
    wanted = TEST_ONLY | set(args.only)
    previous = previous_versions(repo_dir) if test else {}
    if test:
        shutil.rmtree(repo_dir, ignore_errors=True)
    built: list[Built] = []
    for addon_dir in find_addon_dirs():
        if addon_dir.name in TEST_ONLY and not test:
            continue
        if test and addon_dir.name not in wanted:
            continue
        try:
            addon_id, version, text = read_addon_xml(addon_dir)
        except (ValueError, ET.ParseError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        overrides: dict[str, bytes] = {}
        if test:
            version, text, overrides = mark_as_test(addon_dir, version, previous.get(addon_id))
        zip_path = build_zip(addon_dir, addon_id, version, repo_dir / "zips", overrides)
        built.append(Built(addon_id, version, zip_path, text))

    if not built:
        print("ERROR: no add-ons found in addons/", file=sys.stderr)
        return 1

    data = render_addons_xml([b.xml_text for b in built])
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "addons.xml").write_bytes(data)
    # MD5 is the checksum format Kodi expects in addons.xml.md5, not a security measure
    checksum = hashlib.md5(data, usedforsecurity=False).hexdigest()
    (repo_dir / "addons.xml.md5").write_text(checksum, encoding="ascii")

    print(f"{'Addon':<36}{'Version':<10}{'ZIP size':>12}")
    print("-" * 58)
    for b in built:
        print(f"{b.addon_id:<36}{b.version:<10}{human_size(b.zip_path.stat().st_size):>12}")
    print("-" * 58)
    print(f"{len(built)} Addon(s) -> {repo_dir.name}/addons.xml, {repo_dir.name}/addons.xml.md5")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
