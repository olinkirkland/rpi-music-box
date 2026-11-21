"""Wi-Fi setup, against a fake nmcli (nothing on the real network changes)."""

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from musicbox import wifi
from musicbox.cards import CardStore
from musicbox.events import EventBus
from musicbox.taplog import TapLog
from musicbox.web import start_web
from musicbox.wifi import (NetworkManager, Rejections, WifiClient, WifiError, WifiLog,
                           WifiService, WifiWatcher, check_password, check_ssid, serve, split_terse)


class FakeNM:
    """Just enough nmcli: devices, saved profiles, a scan, and connection attempts."""

    def __init__(self):
        self.ethernet = False
        self.wifi_connection = None
        self.profiles = {}  # uuid -> {"name", "ssid", "mode"}
        self.in_range = []  # (ssid, signal, security)
        self.answers = {}  # uuid -> "ok" | "rejected" | "absent"
        self.commands = []
        self.n = 0

    def add_profile(self, ssid, name=None, mode="infrastructure"):
        self.n += 1
        uuid = f"00000000-0000-0000-0000-{self.n:012d}"
        self.profiles[uuid] = {"name": name or ssid, "ssid": ssid, "mode": mode}
        return uuid

    def __call__(self, args, timeout=30):
        self.commands.append(args)
        a = args
        if a[:5] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            lines = [f"eth0:ethernet:{'connected' if self.ethernet else 'unavailable'}:"
                     + ("netplan-eth0" if self.ethernet else ""),
                     "lo:loopback:connected (externally):lo"]
            w = self.wifi_connection
            lines.append(f"wlan0:wifi:{'connected' if w else 'disconnected'}:{w or ''}")
            return 0, "\n".join(lines) + "\n", ""
        if a[:5] == ["-t", "-f", "NAME,UUID,TYPE", "connection", "show"]:
            out = "".join(f"{p['name'].replace(':', chr(92) + ':')}:{u}:802-11-wireless\n"
                          for u, p in self.profiles.items())
            return 0, "netplan-eth0:eeee:802-3-ethernet\n" + out, ""
        if a[:3] == ["-t", "-g", "802-11-wireless.ssid,802-11-wireless.mode"]:
            p = self.profiles.get(a[-1])
            return (0, f"{p['ssid']}\n{p['mode']}\n", "") if p else (10, "", "no such")
        if a[:2] == ["-g", "802-11-wireless.ssid"]:
            p = next((p for p in self.profiles.values() if p["name"] == a[-1]), None)
            return 0, (p["ssid"] + "\n") if p else "\n", ""
        if "wifi" in a and "list" in a:
            return 0, "".join(f"{s.replace(':', chr(92) + ':')}:{sig}:{sec}\n" for s, sig, sec in self.in_range), ""
        if a[:2] == ["connection", "add"]:
            ssid = a[a.index("ssid") + 1]
            uuid = self.add_profile(ssid, a[a.index("con-name") + 1],
                                    "ap" if "ap" in a else "infrastructure")
            return 0, f"Connection '{ssid}' ({uuid}) successfully added.\n", ""
        if a[:2] == ["connection", "delete"]:
            target = a[2]
            gone = [u for u, p in self.profiles.items() if u == target or p["name"] == target]
            for u in gone:
                del self.profiles[u]
            return (0, "", "") if gone else (10, "", "Error: unknown connection")
        if a[:1] == ["--wait"] and a[2:4] == ["connection", "up"]:
            target = a[4]
            if target == wifi.HOTSPOT_NAME:
                self.wifi_connection = wifi.HOTSPOT_NAME
                return 0, "", ""
            answer = self.answers.get(target, "absent")
            if answer == "ok":
                self.wifi_connection = self.profiles[target]["name"]
                return 0, "Connection successfully activated", ""
            if answer == "rejected":
                return 4, "", "Error: Connection activation failed: Secrets were required, but not provided."
            return 10, "", f"Error: Connection activation failed: No network with SSID '{self.profiles[target]['ssid']}' found."
        if a[:2] == ["connection", "down"]:
            return 0, "", ""
        return 0, "", ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wifi, "CAPTIVE_DNS", tmp_path / "dnsmasq" / "captive.conf")
    fake = FakeNM()
    t = {"now": 0.0}

    def sleep(seconds):
        t["now"] += seconds

    reboots = []
    service = WifiService(NetworkManager(run=fake), WifiLog(tmp_path / "log.jsonl", clock=lambda: 1795000000.0),
                          Rejections(tmp_path / "rej.json"), clock=lambda: t["now"], sleep=sleep,
                          reboot=lambda: reboots.append(True))
    return service, fake, t, reboots


def test_split_terse_handles_escaped_colons():
    assert split_terse(r"My\:Net:80:WPA2") == ["My:Net", "80", "WPA2"]
    assert split_terse(r"a\\b::x") == ["a\\b", "", "x"]


@pytest.mark.parametrize("bad", ["", " ", "x" * 33, "bad\nname", None])
def test_ssid_checks(bad):
    with pytest.raises(WifiError):
        check_ssid(bad)


def test_password_checks():
    assert check_password("") == "" and check_password(None) == ""
    assert check_password("12345678") == "12345678"
    for bad in ("short", "x" * 64, "has\ttab1"):
        with pytest.raises(WifiError):
            check_password(bad)


def test_connectivity_and_lists(env):
    service, fake, t, _ = env
    nm = service.nm
    assert nm.connectivity() == {"online": False, "ethernet": False, "wifi": None, "setup": False}
    fake.ethernet = True
    assert nm.connectivity()["online"] and nm.connectivity()["ethernet"]
    u = fake.add_profile("Home:Net", name="netplan-home")
    fake.add_profile(wifi.SETUP_SSID, name=wifi.HOTSPOT_NAME, mode="ap")
    assert nm.saved() == [{"uuid": u, "name": "netplan-home", "ssid": "Home:Net"}]
    fake.in_range = [("Home:Net", "40", "WPA2"), ("Home:Net", "70", "WPA2"), ("Cafe", "55", ""),
                     (wifi.SETUP_SSID, "90", "")]
    assert nm.scan() == [{"ssid": "Home:Net", "signal": 70, "secure": True},
                         {"ssid": "Cafe", "signal": 55, "secure": False}]


def test_rejected_twice_in_a_row_deletes_the_network(env):
    service, fake, t, _ = env
    u = fake.add_profile("Home")
    fake.in_range = [("Home", "70", "WPA2")]
    fake.answers[u] = "rejected"
    assert service.try_saved_networks() is False
    assert u in fake.profiles and service.rejections.count(u) == 1
    assert service.try_saved_networks() is False
    assert u not in fake.profiles
    messages = [e["message"] for e in service.log.entries()]
    assert "removed from the saved networks" in messages[0] and "1 of 2" in messages[1]


def test_out_of_range_is_never_deleted_and_success_clears(env):
    service, fake, t, _ = env
    u = fake.add_profile("Home")
    for _ in range(5):
        service.try_saved_networks()  # not in range: not even tried
    assert u in fake.profiles and service.rejections.count(u) == 0
    fake.in_range = [("Home", "70", "WPA2")]
    fake.answers[u] = "rejected"
    service.try_saved_networks()
    fake.answers[u] = "ok"
    assert service.try_saved_networks() is True and service.rejections.count(u) == 0


def test_start_up_online_with_ethernet(env):
    service, fake, t, _ = env
    fake.ethernet = True
    assert service.start_up() == "online"
    assert not any("ap" in c for c in fake.commands)


def test_start_up_falls_back_to_setup_after_the_wait(env):
    service, fake, t, _ = env
    fake.in_range = [("Neighbour", "60", "WPA2")]
    assert service.start_up() == "setup"
    assert t["now"] >= wifi.BOOT_WAIT
    assert wifi.CAPTIVE_DNS.read_text() == f"address=/#/{wifi.HOTSPOT_ADDRESS}\n"
    assert service.setup_networks == [{"ssid": "Neighbour", "signal": 60, "secure": True}]
    assert fake.wifi_connection == wifi.HOTSPOT_NAME
    assert "No network after 3 minutes" in service.log.entries()[0]["message"]


def test_start_up_joins_a_saved_network_that_comes_back(env):
    service, fake, t, _ = env
    u = fake.add_profile("Home")
    fake.answers[u] = "ok"
    original = service.nm.scan

    def scan_after_a_minute(rescan=True):
        if t["now"] >= 60:
            fake.in_range = [("Home", "70", "WPA2")]
        return original(rescan)

    service.nm.scan = scan_after_a_minute
    assert service.start_up() == "online" and t["now"] < wifi.BOOT_WAIT


def test_requests(env, monkeypatch):
    service, fake, t, reboots = env
    assert service.handle({"cmd": "add", "ssid": "Home", "password": "12345678"}) == {"ok": True}
    status = service.handle({"cmd": "status"})
    assert [n["ssid"] for n in status["saved"]] == ["Home"]
    assert "password" not in json.dumps(status).lower().replace("passwords", "")
    with pytest.raises(WifiError):
        service.handle({"cmd": "setup", "ssid": "Home", "password": "12345678"})  # not in setup mode
    service.mode = "setup"
    monkeypatch.setattr(wifi.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    assert service.handle({"cmd": "setup", "ssid": "Cafe", "password": ""}) == {"ok": True, "restarting": True}
    assert reboots == [True]
    uuid = status["saved"][0]["uuid"]
    assert service.handle({"cmd": "delete", "uuid": uuid}) == {"ok": True}
    with pytest.raises(WifiError):
        service.handle({"cmd": "delete", "uuid": uuid})
    with pytest.raises(WifiError):
        service.handle({"cmd": "add", "ssid": "Home", "password": "short"})


def test_socket_round_trip(env, tmp_path):
    service, fake, t, _ = env
    path = tmp_path / "run" / "wifi.sock"
    server = serve(service, path=path, group="nogroup-does-not-exist")
    try:
        client = WifiClient(path, timeout=5)
        assert client.call("status")["mode"] == "starting"
        with pytest.raises(WifiError, match="unknown request"):
            client.call("explode")
    finally:
        server.close()
    with pytest.raises(WifiError, match="isn't running"):
        WifiClient(tmp_path / "none.sock", timeout=1).call("status")


class FakeWatcher:
    def __init__(self, mode):
        self.status = {"mode": mode, "saved": [], "log": []}
        self.calls = []
        self.client = self

    @property
    def in_setup(self):
        return self.status["mode"] == "setup"

    def refresh(self):
        return self.status

    def call(self, cmd, **args):
        self.calls.append((cmd, args))
        return {"ok": True, "networks": []}


@pytest.fixture
def web(tmp_path):
    def start(mode):
        (tmp_path / "m").mkdir(exist_ok=True)
        watcher = FakeWatcher(mode)
        srv = start_web(TapLog(tmp_path / "t.db"), CardStore(tmp_path / "c.json", tmp_path / "m"),
                        EventBus(), host="127.0.0.1", port=0, wifi=watcher)
        servers.append(srv)
        return watcher, f"http://127.0.0.1:{srv.server_address[1]}"

    servers = []
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


class NoRedirect:
    pass


def get_raw(url):
    import http.client
    from urllib.parse import urlparse
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.request("GET", u.path)
    r = conn.getresponse()
    return r.status, r.getheader("Location"), r.read()


def test_phones_are_sent_to_the_setup_page_only_in_setup_mode(web):
    watcher, base = web("setup")
    for probe in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt"):
        status, location, _ = get_raw(base + probe)
        assert status == 302 and location == f"http://{wifi.HOTSPOT_ADDRESS}/"
    assert get_raw(base + "/")[0] == 200
    assert get_raw(base + "/api/wifi")[0] == 200
    watcher.status["mode"] = "online"
    assert get_raw(base + "/generate_204")[0] == 404


def test_wifi_endpoints(web):
    watcher, base = web("online")

    def post(path, body, headers=None):
        req = Request(base + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            return e.code, json.load(e)

    assert post("/api/wifi/networks", {"ssid": "Home", "password": "12345678"})[0] == 200
    assert watcher.calls[-1] == ("add", {"ssid": "Home", "password": "12345678", "hidden": False})
    assert post("/api/wifi/setup", {"ssid": "Home", "password": ""})[0] == 200
    assert watcher.calls[-1][0] == "setup"
    assert post("/api/wifi/networks", {"ssid": "X"}, {"Origin": "http://evil.example"})[0] == 403
    req = Request(base + "/api/wifi/networks?uuid=not-a-uuid", method="DELETE")
    with pytest.raises(HTTPError) as e:
        urlopen(req)
    assert e.value.code == 400


def test_power_requests(env, monkeypatch, tmp_path):
    service, fake, t, reboots = env
    offs = []
    service.poweroff = lambda: offs.append(True)
    monkeypatch.setattr(wifi.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    assert service.handle({"cmd": "power", "action": "restart"}) == {"ok": True, "action": "restart"}
    assert reboots == [True] and offs == []
    assert service.handle({"cmd": "power", "action": "shutdown"}) == {"ok": True, "action": "shutdown"}
    assert offs == [True] and reboots == [True]
    with pytest.raises(WifiError):
        service.handle({"cmd": "power", "action": "explode"})
    assert "Shutting down" in service.log.entries()[0]["message"]


class PowerController:
    def __init__(self):
        self.stopped = 0

    def stop_for_power(self):
        self.stopped += 1


def test_power_endpoints_stop_music_first(tmp_path):
    (tmp_path / "m").mkdir()
    watcher = FakeWatcher("online")
    controller = PowerController()
    srv = start_web(TapLog(tmp_path / "t.db"), CardStore(tmp_path / "c.json", tmp_path / "m"), EventBus(),
                    host="127.0.0.1", port=0, wifi=watcher, controller=controller)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def post(path, headers=None):
            req = Request(base + path, data=b"{}", method="POST",
                          headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urlopen(req) as r:
                    return r.status
            except HTTPError as e:
                return e.code

        assert post("/api/system/restart") == 200
        assert controller.stopped == 1 and watcher.calls[-1] == ("power", {"action": "restart"})
        assert post("/api/system/shutdown", {"Origin": "http://evil.example"}) == 403
        assert controller.stopped == 1 and watcher.calls[-1][1] == {"action": "restart"}
        assert post("/api/system/shutdown") == 200 and watcher.calls[-1] == ("power", {"action": "shutdown"})
    finally:
        srv.shutdown()
        srv.server_close()
