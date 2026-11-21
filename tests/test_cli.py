"""The command line: every command can be reached, and list/remove work."""

import json

import pytest

from musicbox import cli


@pytest.fixture
def config(tmp_path, monkeypatch):
    music = tmp_path / "music"
    (music / "Story").mkdir(parents=True)
    for name in ("1.mp3", "2.mp3"):
        (music / "Story" / name).write_bytes(b"")
    cards = tmp_path / "cards.json"
    cards.write_text(json.dumps({"aa11": "Story", "bb22": {"tracks": ["Story/2.mp3"], "repeat": True}}))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'music_dir = "{music}"\ncards_file = "{cards}"\n')
    monkeypatch.setenv("MUSICBOX_CONFIG", str(cfg))
    return cards


@pytest.mark.parametrize("command", ["run", "assign", "scan", "list", "remove"])
def test_every_command_has_a_handler(command):
    # Parsing alone would not notice a handler that has gone missing.
    with pytest.raises(SystemExit) as e:
        cli.main([command, "--help"])
    assert e.value.code == 0


def test_list_and_remove(config, capsys):
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "aa11  Story/1.mp3  (+1 more)" in out and "bb22  Story/2.mp3  repeat" in out
    assert cli.main(["remove", "AA11"]) == 0
    assert "aa11" not in json.loads(config.read_text())
    assert cli.main(["remove", "aa11"]) == 1
