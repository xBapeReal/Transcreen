import json

from transcreen.config import Config


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = Config(target_lang="de", ocr_lang="tr", idle_poll=0.1, min_text_height=14)
    cfg.save()
    assert Config.load() == cfg
    raw = json.loads((tmp_path / "Transcreen" / "config.json").read_text(encoding="utf-8"))
    assert raw["target_lang"] == "de"


def test_load_ignores_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "Transcreen"
    folder.mkdir()
    (folder / "config.json").write_text('{"target_lang": "fr", "mystery": 1}', encoding="utf-8")
    assert Config.load().target_lang == "fr"


def test_load_corrupt_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "Transcreen"
    folder.mkdir()
    (folder / "config.json").write_text("{not json", encoding="utf-8")
    assert Config.load() == Config()


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert Config.load() == Config()
