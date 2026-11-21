"""Wi-Fi: saved networks, and the "Tap to Play Setup" network when there's none.

Runs as its own service (tap-to-play-wifi.service, as root, because changing
networks needs it), apart from the music box, so a Wi-Fi problem can never
stop the music. It drives NetworkManager through `nmcli`.

On start-up:
  1. Wait up to BOOT_WAIT for a network. Ethernet with an address counts.
     Meanwhile, try the saved Wi-Fi networks that are in range, about once a
     minute. A password that's rejected REJECTIONS_TO_DELETE times in a row
     deletes that saved network; one that's simply out of range never is.
  2. Still nothing: host the open "Tap to Play Setup" network. Its DNS sends
     every name to the box, so a joining phone opens the setup page. It stays
     in setup until someone enters Wi-Fi details; then they're saved and the
     box restarts.

Every error goes to a timestamped log (LOG_FILE), shown on the setup page
and in Settings. Saved passwords are never sent back to anyone.

The music box talks to this service over a Unix socket (SOCKET), one JSON
request and reply per connection: {"cmd": "status"}, {"cmd": "scan"},
{"cmd": "add", "ssid", "password"}, {"cmd": "delete", "uuid"},
{"cmd": "setup", "ssid", "password"}, and {"cmd": "power", "action":
"restart" | "shutdown"} (it's the box's only service with the permission).
"""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SETUP_SSID = "Tap to Play Setup"
HOTSPOT_NAME = "tap-to-play-setup"  # the NetworkManager connection for it
HOTSPOT_ADDRESS = "10.42.0.1"
PROFILE_PREFIX = "Tap to Play: "  # names of networks saved from the page
BOOT_WAIT = 180.0  # seconds to wait for a network before setup mode
RETRY_EVERY = 60.0  # seconds between tries of the saved networks while waiting
REJECTIONS_TO_DELETE = 2
WIFI_DEVICE = "wlan0"

STATE_DIR = Path("/var/lib/tap-to-play")
LOG_FILE = STATE_DIR / "wifi-log.jsonl"
REJECTIONS_FILE = STATE_DIR / "wifi-rejections.json"
SOCKET = Path("/run/tap-to-play/wifi.sock")
CAPTIVE_DNS = Path("/etc/NetworkManager/dnsmasq-shared.d/tap-to-play-captive.conf")
MAX_LOG = 300


class WifiError(Exception):
    """Something the page can show."""


def check_ssid(ssid) -> str:
    if not isinstance(ssid, str) or not ssid.strip():
        raise WifiError("enter the network's name")
    if len(ssid.encode()) > 32:
        raise WifiError("a Wi-Fi name is at most 32 bytes long")
    if any(ord(c) < 32 for c in ssid):
        raise WifiError("the name has control characters")
    return ssid


def check_password(password) -> str:
    if password in (None, ""):
        return ""  # an open network
    if not isinstance(password, str) or not 8 <= len(password) <= 63:
        raise WifiError("a Wi-Fi password is 8 to 63 characters (leave it empty for an open network)")
    if any(ord(c) < 32 for c in password):
        raise WifiError("the password has control characters")
    return password


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output: fields joined by ":", with ":"
    and "\\" inside a field escaped by a backslash."""
    fields, field, escaped = [], [], False
    for ch in line:
        if escaped:
            field.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(ch)
    fields.append("".join(field))
    return fields


# --- NetworkManager --------------------------------------------------------------

class NetworkManager:
    """The `nmcli` calls this service needs. `run` is swappable for tests."""

    def __init__(self, run=None, device: str = WIFI_DEVICE) -> None:
        self._run = run or self._run_nmcli
        self.device = device

    @staticmethod
    def _run_nmcli(args: list[str], timeout: float = 30) -> tuple[int, str, str]:
        try:
            r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "nmcli timed out"
        except OSError as e:
            return 127, "", str(e)

    def run(self, *args: str, timeout: float = 30) -> tuple[int, str, str]:
        return self._run(list(args), timeout)

    def connectivity(self) -> dict:
        """{"online", "ethernet", "wifi": ssid or None, "setup": hosting Setup}."""
        rc, out, _ = self.run("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
        state = {"online": False, "ethernet": False, "wifi": None, "setup": False}
        if rc != 0:
            return state
        for line in out.splitlines():
            device, kind, status, connection = (split_terse(line) + ["", "", "", ""])[:4]
            if not status.startswith("connected") or status.startswith("connected (externally)"):
                continue
            if kind == "ethernet":
                state["ethernet"] = state["online"] = True
            elif kind == "wifi":
                if connection == HOTSPOT_NAME:
                    state["setup"] = True
                else:
                    state["online"] = True
                    state["wifi"] = self.ssid_of(connection) or connection
        return state

    def ssid_of(self, connection: str) -> str | None:
        rc, out, _ = self.run("-g", "802-11-wireless.ssid", "connection", "show", connection)
        return out.strip() or None if rc == 0 else None

    def saved(self) -> list[dict]:
        """Saved Wi-Fi networks: [{"uuid", "name", "ssid"}] (not the Setup one)."""
        rc, out, _ = self.run("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
        if rc != 0:
            return []
        found = []
        for line in out.splitlines():
            name, uuid, kind = (split_terse(line) + ["", "", ""])[:3]
            if kind != "802-11-wireless" or name == HOTSPOT_NAME:
                continue
            rc, details, _ = self.run("-t", "-g", "802-11-wireless.ssid,802-11-wireless.mode",
                                      "connection", "show", uuid)
            parts = details.strip().split("\n") if rc == 0 else []
            ssid = parts[0] if parts else ""
            mode = parts[1] if len(parts) > 1 else ""
            if mode == "ap":
                continue
            found.append({"uuid": uuid, "name": name, "ssid": ssid or name})
        return found

    def scan(self, rescan: bool = True) -> list[dict]:
        """Networks in range, strongest first: [{"ssid", "signal", "secure"}]."""
        rc, out, _ = self.run("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                              "ifname", self.device, "--rescan", "yes" if rescan else "no", timeout=45)
        if rc != 0:
            return []
        best: dict[str, dict] = {}
        for line in out.splitlines():
            ssid, signal, security = (split_terse(line) + ["", "", ""])[:3]
            if not ssid or ssid == SETUP_SSID:
                continue
            entry = {"ssid": ssid, "signal": int(signal) if signal.isdigit() else 0,
                     "secure": bool(security.strip() and security.strip() != "--")}
            if ssid not in best or entry["signal"] > best[ssid]["signal"]:
                best[ssid] = entry
        return sorted(best.values(), key=lambda e: -e["signal"])

    def add(self, ssid: str, password: str, hidden: bool = False) -> str:
        """Save a network (replacing any saved with the same name); its uuid."""
        for net in self.saved():
            if net["ssid"] == ssid:
                self.run("connection", "delete", net["uuid"])
        args = ["connection", "add", "type", "wifi", "ifname", self.device,
                "con-name", PROFILE_PREFIX + ssid, "ssid", ssid, "connection.autoconnect", "yes"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        rc, out, err = self.run(*args)
        if rc != 0:
            raise WifiError(f"couldn't save {ssid}: {err.strip() or out.strip()}")
        match = re.search(r"\(([0-9a-f-]{36})\)", out)
        return match.group(1) if match else ""

    def delete(self, uuid: str) -> None:
        rc, _, err = self.run("connection", "delete", uuid)
        if rc != 0:
            raise WifiError(f"couldn't delete that network: {err.strip()}")

    def connect(self, uuid: str, timeout: float = 45) -> str:
        """Try one saved network: "ok", "rejected" (password), "absent" or "failed"."""
        rc, out, err = self.run("--wait", str(int(timeout)), "connection", "up", uuid,
                                timeout=timeout + 15)
        if rc == 0:
            return "ok"
        text = (err + out).lower()
        if "secrets were required" in text or "no secrets" in text or "auth" in text and "fail" in text:
            return "rejected"
        if "no network with ssid" in text or "not found" in text or "not available" in text:
            return "absent"
        return "failed"

    def start_setup_network(self) -> None:
        """Host the open "Tap to Play Setup" network on the Wi-Fi chip."""
        CAPTIVE_DNS.parent.mkdir(parents=True, exist_ok=True)
        # Every name a joining phone looks up points at the box: it then shows
        # the setup page ("sign in to network").
        CAPTIVE_DNS.write_text(f"address=/#/{HOTSPOT_ADDRESS}\n")
        self.run("connection", "delete", HOTSPOT_NAME)
        rc, _, err = self.run(
            "connection", "add", "type", "wifi", "ifname", self.device, "con-name", HOTSPOT_NAME,
            "connection.autoconnect", "no", "ssid", SETUP_SSID, "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg", "ipv4.method", "shared",
            "ipv4.addresses", f"{HOTSPOT_ADDRESS}/24", "ipv6.method", "disabled")
        if rc != 0:
            raise WifiError(f"couldn't create the setup network: {err.strip()}")
        rc, _, err = self.run("--wait", "30", "connection", "up", HOTSPOT_NAME, timeout=45)
        if rc != 0:
            raise WifiError(f"couldn't start the setup network: {err.strip()}")

    def stop_setup_network(self) -> None:
        self.run("connection", "down", HOTSPOT_NAME)
        self.run("connection", "delete", HOTSPOT_NAME)
        CAPTIVE_DNS.unlink(missing_ok=True)


# --- the log and the rejection counts ----------------------------------------------

class WifiLog:
    """Timestamped Wi-Fi events, newest last, as JSON lines."""

    def __init__(self, path: Path = LOG_FILE, clock=time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()

    def add(self, level: str, message: str) -> None:
        log.log(logging.ERROR if level == "error" else logging.INFO, "wifi: %s", message)
        entry = {"time": round(self.clock(), 1), "level": level, "message": message}
        with self._lock:
            entries = self._read()[-(MAX_LOG - 1):] + [entry]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(e) + "\n" for e in entries))
            tmp.replace(self.path)

    def _read(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def entries(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return self._read()[-limit:][::-1]  # newest first


class Rejections:
    """How many times in a row each saved network's password was rejected."""

    def __init__(self, path: Path = REJECTIONS_FILE) -> None:
        self.path = path

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def count(self, uuid: str) -> int:
        return int(self._read().get(uuid, 0))

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    def add(self, uuid: str) -> int:
        data = self._read()
        data[uuid] = int(data.get(uuid, 0)) + 1
        self._write(data)
        return data[uuid]

    def clear(self, uuid: str) -> None:
        data = self._read()
        if data.pop(uuid, None) is not None:
            self._write(data)


# --- the service ---------------------------------------------------------------------

class WifiService:
    def __init__(self, nm: NetworkManager, wifi_log: WifiLog, rejections: Rejections,
                 clock=time.monotonic, sleep=time.sleep, reboot=None, poweroff=None) -> None:
        self.nm = nm
        self.log = wifi_log
        self.rejections = rejections
        self.clock = clock
        self.sleep = sleep
        self.reboot = reboot or (lambda: subprocess.run(["systemctl", "reboot"], check=False))
        self.poweroff = poweroff or (lambda: subprocess.run(["systemctl", "poweroff"], check=False))
        self.mode = "starting"  # "starting" (boot wait), "online" or "setup"
        self.setup_networks: list[dict] = []  # what was in range when setup began
        self._lock = threading.RLock()

    # -- start-up ----------------------------------------------------------------

    def try_saved_networks(self) -> bool:
        """Try each saved network that's in range; True once one connects."""
        in_range = {n["ssid"] for n in self.nm.scan()}
        for net in self.nm.saved():
            if net["ssid"] not in in_range:
                continue
            result = self.nm.connect(net["uuid"])
            if result == "ok":
                self.rejections.clear(net["uuid"])
                self.log.add("info", f"Connected to {net['ssid']}.")
                return True
            if result == "rejected":
                count = self.rejections.add(net["uuid"])
                if count >= REJECTIONS_TO_DELETE:
                    self.nm.delete(net["uuid"])
                    self.rejections.clear(net["uuid"])
                    self.log.add("error", f"The password for {net['ssid']} was rejected again, "
                                          f"so it was removed from the saved networks.")
                else:
                    self.log.add("error", f"The password for {net['ssid']} was rejected "
                                          f"({count} of {REJECTIONS_TO_DELETE}; it's removed after "
                                          f"{REJECTIONS_TO_DELETE} in a row).")
            elif result == "failed":
                self.log.add("error", f"Couldn't connect to {net['ssid']}.")
        return False

    def start_up(self) -> str:
        """Wait for a network (trying the saved ones), else start setup mode."""
        deadline = self.clock() + BOOT_WAIT
        next_try = self.clock()
        while self.clock() < deadline:
            if self.nm.connectivity()["online"]:
                with self._lock:
                    self.mode = "online"
                return self.mode
            if self.clock() >= next_try:
                next_try = self.clock() + RETRY_EVERY
                if self.try_saved_networks():
                    with self._lock:
                        self.mode = "online"
                    return self.mode
            self.sleep(5)
        if self.nm.connectivity()["online"]:
            with self._lock:
                self.mode = "online"
            return self.mode
        self.begin_setup("No network after 3 minutes")
        return self.mode

    def begin_setup(self, why: str) -> None:
        with self._lock:
            self.setup_networks = self.nm.scan()  # can't scan once hosting the network
            try:
                self.nm.start_setup_network()
            except WifiError as e:
                self.log.add("error", str(e))
                return
            self.mode = "setup"
            self.log.add("info", f"{why}: started the “{SETUP_SSID}” network.")

    # -- requests from the music box --------------------------------------------

    def handle(self, request: dict) -> dict:
        cmd = request.get("cmd")
        with self._lock:
            if cmd == "status":
                return self.status()
            if cmd == "scan":
                return {"networks": self.setup_networks if self.mode == "setup" else self.nm.scan()}
            if cmd == "add":
                ssid = check_ssid(request.get("ssid"))
                password = check_password(request.get("password"))
                uuid = self.nm.add(ssid, password, bool(request.get("hidden")))
                self.rejections.clear(uuid)
                self.log.add("info", f"Saved the network {ssid}.")
                return {"ok": True}
            if cmd == "delete":
                uuid = request.get("uuid")
                net = next((n for n in self.nm.saved() if n["uuid"] == uuid), None)
                if net is None:
                    raise WifiError("no such saved network")
                self.nm.delete(uuid)
                self.rejections.clear(uuid)
                self.log.add("info", f"Removed the network {net['ssid']}.")
                return {"ok": True}
            if cmd == "setup":
                if self.mode != "setup":
                    raise WifiError("the box isn't in setup mode")
                ssid = check_ssid(request.get("ssid"))
                password = check_password(request.get("password"))
                self.nm.add(ssid, password, bool(request.get("hidden")))
                self.log.add("info", f"Saved {ssid} from the setup page; restarting to join it.")
                threading.Timer(3.0, self.reboot).start()  # after the reply reaches the phone
                return {"ok": True, "restarting": True}
            if cmd == "power":
                action = request.get("action")
                if action not in ("restart", "shutdown"):
                    raise WifiError("power action must be restart or shutdown")
                self.log.add("info", "Restarting, as asked from the page." if action == "restart"
                             else "Shutting down, as asked from the page.")
                # A moment's delay so the reply reaches the page first.
                threading.Timer(2.0, self.reboot if action == "restart" else self.poweroff).start()
                return {"ok": True, "action": action}
            raise WifiError(f"unknown request: {cmd}")

    def status(self) -> dict:
        conn = self.nm.connectivity()
        saved = [{**n, "rejections": self.rejections.count(n["uuid"])} for n in self.nm.saved()]
        return {"mode": self.mode, "setup_ssid": SETUP_SSID, "ethernet": conn["ethernet"],
                "wifi": conn["wifi"], "online": conn["online"], "saved": saved,
                "log": self.log.entries()}


# --- the socket server ---------------------------------------------------------------

def serve(service: WifiService, path: Path = SOCKET, group: str = "berry") -> socket.socket:
    """Answer requests on a Unix socket only `group` (the music box) can use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    try:
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, 0, gid)
        os.chown(path.parent, 0, gid)
        os.chmod(path.parent, 0o750)
    except (KeyError, PermissionError):
        log.warning("couldn't hand the socket to group %s", group)
    os.chmod(path, 0o660)
    server.listen(4)

    def handle(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            try:
                raw = b""
                while not raw.endswith(b"\n") and len(raw) < 16384:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                request = json.loads(raw or b"{}")
                reply = service.handle(request if isinstance(request, dict) else {})
            except WifiError as e:
                reply = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 - always answer, never die
                log.exception("wifi request failed")
                reply = {"error": f"internal error: {e}"}
            try:
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass

    def loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, name="wifi-socket", daemon=True).start()
    return server


class WifiClient:
    """Used by the music box (as berry) to talk to the Wi-Fi service."""

    def __init__(self, path: Path = SOCKET, timeout: float = 70) -> None:
        self.path = path
        self.timeout = timeout

    def call(self, cmd: str, **args) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.path))
                s.sendall(json.dumps({"cmd": cmd, **args}).encode() + b"\n")
                raw = b""
                while not raw.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
        except (OSError, socket.timeout) as e:
            raise WifiError(f"the Wi-Fi service isn't running ({e})") from None
        reply = json.loads(raw or b"{}")
        if "error" in reply:
            raise WifiError(reply["error"])
        return reply


class WifiWatcher:
    """In the music box: keeps the Wi-Fi service's status current, tells open
    pages when it changes ("wifi" event), and knows when the box is in setup
    mode (so the web server can send phones to the setup page)."""

    def __init__(self, client: WifiClient, events=None, every: float = 5.0) -> None:
        self.client = client
        self.events = events
        self.every = every
        self.status: dict = {"mode": "unavailable"}
        self._last = None

    @property
    def in_setup(self) -> bool:
        return self.status.get("mode") == "setup"

    def refresh(self) -> dict:
        try:
            status = self.client.call("status")
        except WifiError as e:
            status = {"mode": "unavailable", "error": str(e)}
        self.status = status
        key = json.dumps(status, sort_keys=True)
        if key != self._last:
            self._last = key
            if self.events is not None:
                self.events.publish("wifi", status)
        return status

    def start(self) -> None:
        def loop() -> None:
            while True:
                self.refresh()
                time.sleep(self.every)

        threading.Thread(target=loop, name="wifi-watch", daemon=True).start()


# --- entry point ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap-to-play-wifi", description=__doc__.split("\n")[0])
    parser.add_argument("--setup-now", action="store_true",
                        help="skip the wait and start the setup network (for testing; Ethernet stays up)")
    parser.add_argument("--group", default="berry", help="group allowed to use the socket")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    service = WifiService(NetworkManager(), WifiLog(), Rejections())
    serve(service, group=args.group)
    service.nm.stop_setup_network()  # a leftover from before a restart
    if args.setup_now:
        service.begin_setup("Started by hand")
    else:
        service.start_up()
    log.info("wifi: mode %s", service.mode)
    while True:
        time.sleep(3600)
