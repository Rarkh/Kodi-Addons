"""Tests of tools/build_repo.py: ZIPs, addons.xml and md5 for the Kodi repository."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import build_repo as br
import pytest

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


def test_test_channel_builds_everything_into_repo_test(
    repo: Path, capsys: pytest.CaptureFixture[str]
):
    write_addon(repo / "addons", "plugin.a")
    write_addon(repo / "addons", "repository.rarkh.test", "1.0.0")
    assert br.main(["--channel", "test"]) == 0
    out = repo / "repo-test"
    data = (out / "addons.xml").read_bytes()
    assert [a.get("id") for a in ET.fromstring(data)] == ["plugin.a", "repository.rarkh.test"]
    assert (out / "addons.xml.md5").read_text(encoding="ascii") == hashlib.md5(data).hexdigest()
    assert (out / "zips" / "plugin.a" / "plugin.a-1.0.0.zip").is_file()
    assert not (repo / "repo").exists()
    assert "repo-test/addons.xml" in capsys.readouterr().out


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
