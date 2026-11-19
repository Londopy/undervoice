import sys
import tomllib

import pytest

from undervoice._config import defaults, folder, load, save


def test_round_trip_keeps_every_kind_of_value(tmp_path):
    settings = defaults()
    settings["station"] = {"call": "W1AW", "grid": "FN31pr", "status": r'SAYS "HI" \ OK'}
    settings["radio"] = {"mic": "Mikrofon (USB Audio) ü", "ptt": "COM5:dtr", "level": -18.0}
    path = tmp_path / "undervoice" / "config.toml"
    save(settings, path)
    assert load(path) == settings


def test_missing_file_gives_defaults(tmp_path):
    assert load(tmp_path / "nope.toml") == defaults()


def test_saved_values_override_defaults_but_keep_the_rest(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[updater]\nchannel = "beta"\n', encoding="utf-8")
    updater = load(path)["updater"]
    assert updater["channel"] == "beta"
    assert updater["enabled"] is True


def test_broken_file_is_reported(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[station\ncall = ", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load(path)


def test_portable_copy_keeps_config_next_to_the_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "undervoice.exe"))
    assert folder() != tmp_path
    (tmp_path / "portable").touch()
    assert folder() == tmp_path
