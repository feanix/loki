"""Tests for loki.settings (schema v2: the file list with per-file cap and bake toggle)."""

import json
import os
import threading

import pytest

from loki import core
from loki.settings import FileSetting, Settings


def test_round_trip_keeps_order_caps_and_toggles(tmp_path):
    p = tmp_path / "s.json"
    s = Settings.load(p)
    assert s.files() == [] and s.imagetopaa_path is None
    keys = s.add_files([tmp_path / "b.png", tmp_path / "sub" / "a.png", tmp_path / "c.png"])
    assert len(keys) == 3
    s.set_bake([keys[1]], True)
    s.set_max_size([keys[0], keys[2]], 512)
    s.set_max_size([keys[2]], None)
    s.imagetopaa_path = "T.exe"
    s.save()
    again = Settings.load(p)
    assert [(os.path.basename(f.path), f.max_size, f.bake) for f in again.files()] == [
        ("b.png", 512, False), ("a.png", None, True), ("c.png", None, False)
    ]  # fmt: skip
    assert again.imagetopaa_path == "T.exe"
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == 2


def test_add_is_by_key_so_a_second_add_is_a_no_op(tmp_path):
    s = Settings(path=tmp_path / "s.json")
    assert len(s.add_files([tmp_path / "A.png"])) == 1
    assert s.add_files([tmp_path / "A.png"]) == []
    if os.name == "nt":  # Windows paths compare case- and slash-insensitively
        assert s.add_files([str(tmp_path / "a.PNG").replace("\\", "/")]) == []
    assert len(s.files()) == 1
    assert s.files()[0].key == core.file_key(tmp_path / "A.png")


def test_mutators_only_touch_the_named_keys_and_remove_forgets(tmp_path):
    s = Settings(path=tmp_path / "s.json")
    k1, k2 = s.add_files([tmp_path / "a.png", tmp_path / "b.png"])
    s.set_max_size([k1], 256)
    s.set_bake([k2], True)
    assert [(f.max_size, f.bake) for f in s.files()] == [(256, False), (None, True)]
    s.remove_files([k1])
    assert [f.key for f in s.files()] == [k2]
    snap = s.files()
    snap.clear()  # a snapshot: editing it never reaches the settings
    assert len(s.files()) == 1


def test_bad_values_are_rejected_or_skipped(tmp_path):
    s = Settings(path=tmp_path / "s.json")
    (k,) = s.add_files([tmp_path / "a.png"])
    try:
        s.set_max_size([k], 4096)
    except ValueError:
        pass
    else:
        raise AssertionError("4096 is not a supported cap")
    p = tmp_path / "bad.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "files": [
                    {"path": str(tmp_path / "ok.png"), "max_size": 256, "bake": True},
                    {"path": str(tmp_path / "ok.png"), "max_size": 512},  # duplicate: first wins
                    {"path": str(tmp_path / "odd.png"), "max_size": 999, "bake": "yes"},
                    {"path": 7},
                    "junk",
                    {"max_size": 256},
                ],
            }
        ),
        encoding="utf-8",
    )
    got = Settings.load(p).files()
    assert [(os.path.basename(f.path), f.max_size, f.bake) for f in got] == [
        ("ok.png", 256, True), ("odd.png", None, False)
    ]  # fmt: skip


def test_corrupt_or_missing_settings_give_defaults(tmp_path):
    p = tmp_path / "s.json"
    assert Settings.load(p).files() == []
    for text in ("{not json", '["list"]', '{"files": "nope"}'):
        p.write_text(text, encoding="utf-8")
        assert Settings.load(p).files() == []


def test_a_0_1_0_settings_file_loads_and_folder_fields_are_not_written_back(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(
        json.dumps(
            {
                "folder": "C:/old/tex",
                "include_subfolders": False,
                "max_size": 512,
                "imagetopaa_path": "C:/tools/ImageToPAA.exe",
                "bake": {"c:\\old\\tex": {"a.png": True}},
            }
        ),
        encoding="utf-8",
    )
    s = Settings.load(p)
    assert s.imagetopaa_path == "C:/tools/ImageToPAA.exe" and s.files() == []
    s.add_files([tmp_path / "x.png"])  # any write migrates the file to v2
    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"version", "imagetopaa_path", "converter", "sort", "files"}
    assert "folder" not in p.read_text(encoding="utf-8")


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    s = Settings(path=tmp_path / "d" / "s.json")
    s.add_files([tmp_path / "a.png"])
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["s.json"]


def test_file_setting_key_matches_core(tmp_path):
    f = FileSetting(str(tmp_path / "A.png"))
    assert f.key == core.file_key(f.path) and f.max_size is None and f.bake is False


def test_thread_safe_snapshot_while_editing(tmp_path):
    s = Settings(path=tmp_path / "s.json")
    keys = s.add_files([tmp_path / f"{i}.png" for i in range(8)])
    stop = threading.Event()
    seen = []

    def reader():
        while not stop.is_set():
            seen.append(len(s.files()))

    t = threading.Thread(target=reader)
    t.start()
    for k in keys:
        s.set_bake([k], True)
    stop.set()
    t.join()
    assert set(seen) == {8}


def test_a_0_3_0_settings_file_keeps_every_rows_toggle_after_the_auto_watch_rename(tmp_path):
    """The key stays "bake" on disk (renames only what the user sees): a 0.3.0 file loads intact."""
    p = tmp_path / "v030.json"
    rows = [("a.png", 256, True, "loki"), ("b.png", None, False, "bohemia"), ("c.png", 1024, True, None)]
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "imagetopaa_path": None,
                "converter": "loki",
                "files": [
                    {"path": str(tmp_path / n), "max_size": cap, "bake": on, "made_by": by} for n, cap, on, by in rows
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    s = Settings.load(p)
    assert [(os.path.basename(f.path), f.max_size, f.bake) for f in s.files()] == [
        (n, cap, on) for n, cap, on, _ in rows
    ]
    s.set_bake([s.files()[1].key], True)  # a save writes the same key back, so 0.3.0 can still read it
    assert [f["bake"] for f in json.loads(p.read_text(encoding="utf-8"))["files"]] == [True, True, True]


def test_sort_is_saved_and_old_or_bad_values_load_as_file_ascending(tmp_path):
    p = tmp_path / "s.json"
    assert Settings.load(p).sort == ("file", False)  # no file
    s = Settings.load(p)
    s.set_sort("size", True)
    assert json.loads(p.read_text(encoding="utf-8"))["sort"] == {"column": "size", "descending": True}
    assert Settings.load(p).sort == ("size", True)
    with pytest.raises(ValueError):
        s.set_sort("nope", False)
    data = json.loads(p.read_text(encoding="utf-8"))
    for stored in (None, "size", {"column": "bogus", "descending": False}, {"column": "size", "descending": "x"}):
        data["sort"] = stored  # 0.3.0 had no key; garbage must not break the load
        p.write_text(json.dumps(data), encoding="utf-8")
        assert Settings.load(p).sort == ("file", False)
    data.pop("sort")
    p.write_text(json.dumps(data), encoding="utf-8")
    assert Settings.load(p).sort == ("file", False)
