import time

from musicbox.player import Player


def test_play_and_stop_with_a_stand_in_command(tmp_path):
    # `sleep -- 30` stands in for mpv: a process that runs until stopped.
    player = Player(command=["sleep"])
    player.play("30")
    assert player.playing
    start = time.monotonic()
    player.stop()
    assert not player.playing
    assert time.monotonic() - start < 2


def test_stop_when_idle_is_harmless():
    Player(command=["sleep"]).stop()
