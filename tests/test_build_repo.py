"""Tests of tools/build_repo.py: ZIPs, addons.xml and md5 for the Kodi repository."""

from __future__ import annotations

import hashlib
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import build_repo as br
import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "build_repo.py"


def write_addon(
    root: Path, addon_id: str, version: str = "1.0.0", declaration: bool = True
) -> Path:
    folder = root / addon_id
    folder.mkdir(parents=True)
    head = '<?xml version="1.0" encoding="UTF-8"?>\r\n' if declaration else ""
    xml = f'{head}<addon id="{addon_id}" version="{version}" name="X"/>\r\n'
    (folder / "addon.xml").write_bytes(xml.encode("utf-8"))
    (folder / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "main.cpython-315.pyc").write_bytes(b"x")
    (folder / "old.pyc").write_bytes(b"x")
    (folder / "old.PYO").write_bytes(b"x")
    (folder / ".vscode").mkdir()
    (folder / ".vscode" / "settings.json").write_text("{}", encoding="utf-8")
    (folder / "resources").mkdir()
    (folder / "resources" / "settings.xml").write_text("<settings/>", encoding="utf-8")
    return folder


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake repository root; the build paths of the script point into it."""
    addons = tmp_path / "addons"
    addons.mkdir()
    repo_dir = tmp_path / "repo"
    monkeypatch.setattr(br, "ADDONS_DIR", addons)
    monkeypatch.setattr(br, "REPO_DIR", repo_dir)
    monkeypatch.setattr(br, "ZIPS_DIR", repo_dir / "zips")
    monkeypatch.setattr(br, "TEST_REPO_DIR", tmp_path / "repo-test")
    return tmp_path


def test_read_addon_xml_returns_id_version_and_text(tmp_path: Path):
    folder = write_addon(tmp_path, "plugin.x", "2.3.4")
    addon_id, version, text = br.read_addon_xml(folder)
    assert (addon_id, version) == ("plugin.x", "2.3.4")
    assert text == '<addon id="plugin.x" version="2.3.4" name="X"/>'
    assert "\r" not in text


def test_read_addon_xml_without_declaration(tmp_path: Path):
    folder = write_addon(tmp_path, "plugin.x", declaration=False)
    assert br.read_addon_xml(folder)[2].startswith("<addon ")


def test_read_addon_xml_rejects_missing_version(tmp_path: Path):
    folder = tmp_path / "plugin.x"
    folder.mkdir()
    (folder / "addon.xml").write_text('<addon id="plugin.x"/>', encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        br.read_addon_xml(folder)


def test_read_addon_xml_rejects_folder_mismatch(tmp_path: Path):
    folder = write_addon(tmp_path, "plugin.other")
    renamed = tmp_path / "plugin.wrong"
    folder.rename(renamed)
    with pytest.raises(ValueError, match="does not match"):
        br.read_addon_xml(renamed)


def test_read_addon_xml_rejects_broken_xml(tmp_path: Path):
    folder = tmp_path / "plugin.x"
    folder.mkdir()
    (folder / "addon.xml").write_text("<addon", encoding="utf-8")
    with pytest.raises(ET.ParseError):
        br.read_addon_xml(folder)


def test_included_files_skip_caches_and_hidden_tools(tmp_path: Path):
    folder = write_addon(tmp_path, "plugin.x")
    names = [rel.as_posix() for _, rel in br.included_files(folder)]
    assert names == ["addon.xml", "main.py", "resources/settings.xml"]


def test_build_zip_has_addon_folder_as_root(repo: Path):
    folder = write_addon(repo / "addons", "plugin.x")
    zip_path = br.build_zip(folder, "plugin.x", "1.0.0")
    assert zip_path == repo / "repo" / "zips" / "plugin.x" / "plugin.x-1.0.0.zip"
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == [
            "plugin.x/addon.xml",
            "plugin.x/main.py",
            "plugin.x/resources/settings.xml",
        ]
        assert zf.getinfo("plugin.x/main.py").date_time == (2000, 1, 1, 0, 0, 0)


def test_build_zip_copies_assets(repo: Path):
    folder = write_addon(repo / "addons", "plugin.x")
    (folder / "icon.png").write_bytes(b"png")
    (folder / "fanart.jpg").write_bytes(b"jpg")
    br.build_zip(folder, "plugin.x", "1.0.0")
    out = repo / "repo" / "zips" / "plugin.x"
    assert (out / "icon.png").read_bytes() == b"png"
    assert (out / "fanart.jpg").read_bytes() == b"jpg"


def test_render_addons_xml_is_valid_xml():
    data = br.render_addons_xml(['<addon id="a" version="1"/>', '<addon id="b" version="2"/>'])
    root = ET.fromstring(data)
    assert [a.get("id") for a in root] == ["a", "b"]
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')


def test_render_addons_xml_rejects_invalid_entries():
    with pytest.raises(ET.ParseError):
        br.render_addons_xml(["<addon"])


def test_find_addon_dirs_ignores_folders_without_addon_xml(repo: Path):
    write_addon(repo / "addons", "plugin.b")
    write_addon(repo / "addons", "plugin.a")
    (repo / "addons" / "not-an-addon").mkdir()
    assert [d.name for d in br.find_addon_dirs()] == ["plugin.a", "plugin.b"]


def test_human_size_units():
    assert br.human_size(1536) == "1.5 KB"
    assert br.human_size(3 * 1024 * 1024) == "3.00 MB"


def test_main_builds_zips_and_index(repo: Path, capsys: pytest.CaptureFixture[str]):
    write_addon(repo / "addons", "plugin.a", "1.0.0")
    write_addon(repo / "addons", "plugin.b", "2.0.0")
    assert br.main([]) == 0

    out = repo / "repo"
    data = (out / "addons.xml").read_bytes()
    assert [a.get("id") for a in ET.fromstring(data)] == ["plugin.a", "plugin.b"]
    assert (out / "addons.xml.md5").read_text(encoding="ascii") == hashlib.md5(data).hexdigest()
    assert (out / "zips" / "plugin.b" / "plugin.b-2.0.0.zip").is_file()

    printed = capsys.readouterr().out
    assert "plugin.a" in printed
    assert "2.0.0" in printed
    assert "2 Addon(s)" in printed


def test_release_channel_leaves_out_the_test_repository(repo: Path):
    write_addon(repo / "addons", "plugin.a")
    write_addon(repo / "addons", "repository.rarkh.test")
    assert br.main([]) == 0
    ids = [a.get("id") for a in ET.fromstring((repo / "repo" / "addons.xml").read_bytes())]
    assert ids == ["plugin.a"]
    assert not (repo / "repo-test").exists()


def test_test_channel_builds_only_the_test_repo_and_chosen_addons(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    write_addon(repo / "addons", "plugin.a")
    write_addon(repo / "addons", "plugin.b")
    write_addon(repo / "addons", "repository.rarkh.test", "1.0.0")
    stale = repo / "repo-test" / "zips" / "plugin.old"
    stale.mkdir(parents=True)
    assert br.main(["--channel", "test", "--only", "plugin.b"]) == 0
    out = repo / "repo-test"
    data = (out / "addons.xml").read_bytes()
    assert [a.get("id") for a in ET.fromstring(data)] == ["plugin.b", "repository.rarkh.test"]
    assert (out / "addons.xml.md5").read_text(encoding="ascii") == hashlib.md5(data).hexdigest()
    assert (out / "zips" / "plugin.b" / "plugin.b-1.0.0-rc1.zip").is_file()
    assert not (out / "zips" / "plugin.a").exists()
    assert not stale.exists()
    assert not (repo / "repo").exists()
    assert "repo-test/addons.xml" in capsys.readouterr().out


def test_test_channel_without_only_contains_just_the_test_repo(repo: Path):
    write_addon(repo / "addons", "plugin.a")
    write_addon(repo / "addons", "repository.rarkh.test")
    assert br.main(["--channel", "test"]) == 0
    data = (repo / "repo-test" / "addons.xml").read_bytes()
    assert [a.get("id") for a in ET.fromstring(data)] == ["repository.rarkh.test"]


def test_main_fails_on_broken_addon(repo: Path, capsys: pytest.CaptureFixture[str]):
    write_addon(repo / "addons", "plugin.a")
    (repo / "addons" / "plugin.a" / "addon.xml").write_text("<addon", encoding="utf-8")
    assert br.main([]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_main_fails_on_wrong_folder_name(repo: Path, capsys: pytest.CaptureFixture[str]):
    write_addon(repo / "addons", "plugin.a")
    (repo / "addons" / "plugin.a").rename(repo / "addons" / "plugin.z")
    assert br.main([]) == 1
    assert "does not match" in capsys.readouterr().err


def test_main_fails_without_addons(repo: Path, capsys: pytest.CaptureFixture[str]):
    assert br.main([]) == 1
    assert "no add-ons" in capsys.readouterr().err


def test_script_runs_as_command_line_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the real script file, but with __file__ in a temporary tree.

    ROOT is derived from __file__, so the real repo/ stays untouched while the
    file's own code (including the __main__ block) is executed and measured.
    """
    monkeypatch.setattr("sys.argv", ["build_repo.py"])
    write_addon(tmp_path / "addons", "plugin.a")
    code = compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")
    fake_file = tmp_path / "tools" / "build_repo.py"
    with pytest.raises(SystemExit) as exit_info:
        exec(code, {"__name__": "__main__", "__file__": str(fake_file)})
    assert exit_info.value.code == 0
    assert (tmp_path / "repo" / "addons.xml").is_file()


def test_rc_version_counts_up_for_the_same_base_and_restarts_for_a_new_one():
    assert br.rc_version("0.3.4", None) == "0.3.4-rc1"
    assert br.rc_version("0.3.4", "0.3.4-rc1") == "0.3.4-rc2"
    assert br.rc_version("0.3.4", "0.3.4-rc9") == "0.3.4-rc10"
    assert br.rc_version("0.3.5", "0.3.4-rc7") == "0.3.5-rc1"
    assert br.rc_version("0.3.4", "0.3.4") == "0.3.4-rc1"


def test_retag_changes_only_version_and_name_of_the_addon_tag():
    head = '<?xml version="1.0"?>\n<addon id="a" name="Old" version="1.0.0">\n'
    text = head + '<import version="2"/>\n</addon>\n'
    out = br.retag(text, "1.0.1-rc1", "Old (Test)")
    assert '<addon id="a" name="Old (Test)" version="1.0.1-rc1">' in out
    assert '<import version="2"/>' in out


def test_the_test_channel_marks_version_name_and_icon(repo: Path):
    plain = write_addon(repo / "addons", "plugin.b", "1.0.0")
    Image.new("RGBA", (64, 64), (0, 128, 0, 255)).save(plain / "icon.png")
    named = write_addon(repo / "addons", "repository.rarkh.test", "1.0.0")
    (named / "addon.xml").write_text(
        '<addon id="repository.rarkh.test" version="1.0.0" name="R (Test)"/>'
    )
    assert br.main(["--channel", "test", "--only", "plugin.b"]) == 0
    out = repo / "repo-test"
    entries = {a.get("id"): a for a in ET.fromstring((out / "addons.xml").read_bytes())}
    assert entries["plugin.b"].get("version") == "1.0.0-rc1"
    assert entries["plugin.b"].get("name") == "X (Test)"
    assert entries["repository.rarkh.test"].get("name") == "R (Test)"  # not marked twice
    with zipfile.ZipFile(out / "zips" / "plugin.b" / "plugin.b-1.0.0-rc1.zip") as zf:
        inside = zf.read("plugin.b/addon.xml").decode()
        icon = zf.read("plugin.b/icon.png")
    assert 'version="1.0.0-rc1"' in inside
    assert 'name="X (Test)"' in inside
    marked = Image.open(io.BytesIO(icon)).convert("RGB")
    assert marked.getpixel((256, 400)) == (200, 30, 30)  # the red band
    assert marked.getpixel((5, 5)) == (0, 128, 0)  # the add-on's own icon
    assert (out / "zips" / "plugin.b" / "icon.png").read_bytes() == icon
    assert (plain / "icon.png").read_bytes() != icon  # the source stays clean
    assert 'version="1.0.0"' in (plain / "addon.xml").read_text()
    # a second build counts up
    assert br.main(["--channel", "test", "--only", "plugin.b"]) == 0
    assert (out / "zips" / "plugin.b" / "plugin.b-1.0.0-rc2.zip").is_file()


def test_an_addon_without_icon_gets_one_made_from_its_name(tmp_path: Path):
    folder = write_addon(tmp_path, "plugin.x")
    icon = Image.open(io.BytesIO(br.test_icon(folder, "Name (Test)"))).convert("RGB")
    assert icon.size == (br.ICON_SIZE, br.ICON_SIZE)
    assert icon.getpixel((5, 5)) == (32, 52, 84)
    assert icon.getpixel((256, 400)) == (200, 30, 30)
    plain = Image.open(io.BytesIO(br.test_icon(folder, "Name"))).convert("RGB")
    assert plain.getpixel((256, 400)) == (200, 30, 30)


def test_previous_versions_reads_an_existing_index(tmp_path: Path):
    assert br.previous_versions(tmp_path) == {}
    (tmp_path / "addons.xml").write_text(
        '<?xml version="1.0"?>\n<addons><addon id="a" version="1.0.0-rc3"/></addons>'
    )
    assert br.previous_versions(tmp_path) == {"a": "1.0.0-rc3"}
