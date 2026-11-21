import json
import socket
import threading
import time

import pytest

from musicbox.box import MusicBox
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.taplog import TapLog
from musicbox.web import start_web

from test_box import FakePlayer, FakeReader


def test_bus_fans_out_and_remembers_now():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish("now", {"state": "playing"})
    assert a.get_nowait() == ("now", {"state": "playing"})
    assert b.get_nowait() == ("now", {"state": "playing"})
    assert bus.now == {"state": "playing"}
    bus.unsubscribe(a)
    bus.publish("tap", {"id": 1})
    assert a.empty() and b.get_nowait() == ("tap", {"id": 1})


def test_bus_drops_a_subscriber_that_stops_reading():
    bus = EventBus(max_queued=2)
    bus.subscribe()
    for i in range(3):
        bus.publish("tap", {"id": i})
    assert bus.subscriber_count == 0


class PlayingPlayer(FakePlayer):
    chime_seconds = 2.0

    def __init__(self):
        super().__init__()
        self.playing = False

    def play(self, track):
        super().play(track)
        self.playing = True
        return True

    def stop(self):
        super().stop()
        self.playing = False


def make(tmp_path, reads, duration=180.0):
    music = tmp_path / "music"
    music.mkdir()
    (music / "Artist - Song.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    cards.assign("aa", music / "Artist - Song.mp3")
    bus = EventBus()
    q = bus.subscribe()
    player = PlayingPlayer()
    box = MusicBox(FakeReader(reads), player, cards, taplog=TapLog(tmp_path / "t.db"),
                   events=bus, clock=lambda: 1000.0, duration_of=lambda p: duration)
    return box, player, bus, q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_now_playing_lifecycle(tmp_path):
    box, player, bus, q = make(tmp_path, [None, "aa", "aa", "aa", None, None, None])
    box.tick()
    box.tick()
    assert box.now["state"] == "playing"
    assert box.now["name"] == "Artist - Song"
    assert box.now["song_starts_at"] == 1002.0  # after the 2 s chime
    assert wait_for(lambda: box.now.get("duration") == 180.0)
    events = drain(q)
    assert [e for e, _ in events].count("tap") == 1
    tap = next(d for e, d in events if e == "tap")
    assert tap["uid"] == "aa" and tap["result"] == "played" and tap["id"] == 1

    player.playing = False  # the song ends with the card still on
    box.tick()
    assert box.now["state"] == "finished"
    for _ in range(4):  # lifted
        box.tick()
    assert box.now == {"state": "idle"}
    assert bus.now == {"state": "idle"}


def test_states_for_unknown_and_waiting_cards(tmp_path):
    box, *_ = make(tmp_path, ["aa", None, None, None, "zz"])
    box.tick()
    assert box.now == {"state": "waiting", "uid": "aa"}
    for _ in range(4):
        box.tick()
    assert box.now == {"state": "unknown", "uid": "zz"}


def test_late_duration_is_ignored_after_a_change(tmp_path):
    gate = threading.Event()

    def slow_duration(path):
        gate.wait(2)
        return 99.0

    box, player, bus, q = make(tmp_path, [None, "aa", None, None, None])
    box.duration_of = slow_duration
    for _ in range(5):
        box.tick()
    assert box.now == {"state": "idle"}
    gate.set()
    time.sleep(0.1)
    assert box.now == {"state": "idle"}


@pytest.fixture
def server(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"")
    cards = CardStore(tmp_path / "cards.json", music)
    bus = EventBus()
    srv = start_web(TapLog(tmp_path / "t.db"), cards, bus, host="127.0.0.1", port=0)
    yield bus, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def read_events(sock, n, timeout=3.0):
    """Read n SSE events from a raw socket: [(event, data), ...]."""
    sock.settimeout(timeout)
    buf, events = b"", []
    while len(events) < n:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            fields = dict(line.split(": ", 1) for line in block.decode().splitlines()
                          if ": " in line and not line.startswith(":"))
            if "event" in fields:
                events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_event_stream_sends_snapshot_then_live_events(server):
    bus, port = server
    bus.publish("now", {"state": "playing", "uid": "aa"})
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
    (event, data), = read_events(sock, 1)
    assert event == "now" and data["state"] == "playing" and "server_time" in data
    assert wait_for(lambda: bus.subscriber_count == 1)
    bus.publish("tap", {"id": 7})
    bus.publish("cards", {})
    assert read_events(sock, 2) == [("tap", {"id": 7}), ("cards", {})]
    sock.close()
    bus.publish("tap", {"id": 8})  # noticed as gone on the next write
    assert wait_for(lambda: (bus.publish("tap", {}), bus.subscriber_count == 0)[1])


def test_card_edits_are_announced(server):
    from urllib.request import Request, urlopen

    bus, port = server
    q = bus.subscribe()
    req = Request(f"http://127.0.0.1:{port}/api/cards/aa11", method="PUT",
                  data=b'{"track": "a.mp3"}', headers={"Content-Type": "application/json"})
    urlopen(req).close()
    assert q.get(timeout=2) == ("cards", {})


def test_event_stream_snapshot_includes_volume(server):
    bus, port = server
    bus.publish("volume", {"level": 60, "steps": [0, 20], "available": True})
    sock = socket.create_connection(("127.0.0.1", port))
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
    events = read_events(sock, 2)
    sock.close()
    assert [e for e, _ in events] == ["now", "volume"]
    assert events[1][1]["level"] == 60
