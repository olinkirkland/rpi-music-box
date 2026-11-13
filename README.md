# rpi-music-box

A Toniebox-style music player for a Raspberry Pi. Put an NFC card on the
reader and its song plays. Take the card off and the song stops.

Each card is one track. There are no buttons, no screen and no app.

## Hardware

- Raspberry Pi 3B+
- PN532 NFC module (V3, the red board with DIP switches), wired over I2C
- NFC cards (MIFARE Classic / NTAG, 13.56 MHz)
- USB speakers
- USB hard drive holding the music

### Wiring the PN532

Set the two DIP switches on the PN532 to **I2C mode: switch 1 ON, switch 2 OFF**.
Then connect the 4-pin header to the Pi:

| PN532 | Raspberry Pi            |
|-------|-------------------------|
| VCC   | Pin 1 (3.3V)            |
| GND   | Pin 6 (GND)             |
| SDA   | Pin 3 (GPIO 2 / SDA)    |
| SCL   | Pin 5 (GPIO 3 / SCL)    |

### Notes on the other parts

- **Speakers:** if they carry audio over USB, they show up as a USB sound card.
  If USB is only for power and they also have a 3.5mm plug, put that plug in the
  Pi's headphone jack. Either way, see [Audio output](#audio-output).
- **Hard drive:** a 2.5" spinning drive can draw more than the Pi's USB ports
  like to give. If it clicks or disconnects, use an enclosure with its own power
  supply or a powered USB hub, and use a good 2.5A power supply for the Pi.

## Setup

Start from Raspberry Pi OS Lite (Bookworm or later), with SSH enabled.

### 1. Install

```bash
git clone https://github.com/olinkirkland/rpi-music-box.git
```

```bash
cd rpi-music-box && ./install.sh
```

This installs mpv, enables I2C, creates a virtualenv in `.venv`, and registers
a `musicbox` systemd service that starts on boot. Reboot once afterwards so the
I2C setting takes effect.

Check that the Pi sees the reader (it should show a device at address `24`):

```bash
i2cdetect -y 1
```

### 2. Mount the hard drive

Find the drive's label or UUID:

```bash
lsblk -f
```

Add a line to `/etc/fstab` so it mounts at `/mnt/music` on every boot. `nofail`
matters: without it the Pi will not boot if the drive is unplugged.

```
UUID=<your-uuid>  /mnt/music  auto  ro,nofail,x-systemd.device-timeout=10  0  0
```

```bash
sudo mkdir -p /mnt/music && sudo mount -a
```

(For an NTFS or exFAT drive, replace `auto` with `ntfs3` or `exfat`.)

### 3. Audio output

Play something to test:

```bash
mpv --no-video /mnt/music/some-song.mp3
```

If the sound comes out of the wrong place, list the devices with
`mpv --audio-device=help` and set `audio_device` in the config file (below).

### 4. Assign cards

Assign one or more tracks. You are asked to tap a card for each in turn:

```bash
.venv/bin/musicbox assign /mnt/music/Beatles/*.mp3
```

Other commands:

```bash
.venv/bin/musicbox list
```

```bash
.venv/bin/musicbox scan
```

```bash
.venv/bin/musicbox remove <uid>
```

`assign` and `scan` need the reader to themselves, so stop the service first
(`sudo systemctl stop musicbox`) and start it again when done. Tapping an
already-assigned card during `assign` reassigns it.

The cards are plain black, so label each one, for example with a printed
sticker or a paint marker.

### 5. Start

```bash
sudo systemctl start musicbox
```

Watch what it is doing (card taps, unknown cards, errors):

```bash
journalctl -u musicbox -f
```

## Configuration

Optional. Copy `config.example.toml` to `~/.config/musicbox/config.toml` and
edit it; the example file documents every setting (music directory, volume,
audio device, card-removal sensitivity). Restart the service after changes.

The card mapping lives in `~/.config/musicbox/cards.json`, a plain
`{"<card uid>": "<path relative to music_dir>"}` file you can also edit by hand.

## Behaviour

- Card placed: its track plays from the start.
- Card removed: playback stops.
- Track ends with the card still on the reader: silence. Lift and replace the
  card to hear it again.
- A different card placed: switches to that card's track.
- Unknown card: nothing plays, and its UID is logged.
- The service restarts itself if it crashes and reconnects if the reader
  glitches.

## Troubleshooting

- **Song cuts out while the card is on the reader:** raise `misses_to_remove`
  in the config (try 5). Also make sure the card lies flat on the antenna.
- **`i2cdetect` shows nothing at 24:** check the DIP switches and wiring, and
  that you rebooted after install.
- **"file is missing" in the log:** the drive is not mounted; check `lsblk` and
  the drive's power.
- **Protecting the SD card:** the box will be switched off by pulling the plug.
  Once everything works, consider enabling the read-only overlay filesystem
  (`sudo raspi-config` → Performance Options → Overlay File System).

## Development

The logic runs without the hardware; only `musicbox/reader.py` touches the PN532.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest
```
