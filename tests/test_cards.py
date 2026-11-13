import pytest

from musicbox.cards import CardStore


@pytest.fixture
def store(tmp_path):
    music = tmp_path / "music"
    (music / "album").mkdir(parents=True)
    (music / "album" / "song.mp3").write_bytes(b"")
    return CardStore(tmp_path / "conf" / "cards.json", music)


def test_assign_and_lookup(store):
    assert store.assign("aa", store.music_dir / "album" / "song.mp3") == "album/song.mp3"
    assert store.track_for("aa") == store.music_dir / "album" / "song.mp3"
    assert store.track_for("bb") is None


def test_path_relative_to_music_dir(store):
    assert store.assign("aa", "album/song.mp3") == "album/song.mp3"


def test_rejects_tracks_outside_music_dir(store, tmp_path):
    outside = tmp_path / "other.mp3"
    outside.write_bytes(b"")
    with pytest.raises(ValueError):
        store.assign("aa", outside)


def test_rejects_missing_track(store):
    with pytest.raises(FileNotFoundError):
        store.assign("aa", "album/nope.mp3")


def test_sees_changes_made_by_another_process(store):
    assert store.track_for("aa") is None
    CardStore(store.cards_file, store.music_dir).assign("aa", "album/song.mp3")
    assert store.track_for("aa") is not None


def test_remove(store):
    store.assign("aa", "album/song.mp3")
    assert store.remove("aa") is True
    assert store.remove("aa") is False
    assert store.all() == {}
