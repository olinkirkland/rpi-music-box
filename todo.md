# To do

## Hardware

- [ ] Set the PN532 DIP switches to I2C (switch 1 ON, switch 2 OFF)
- [ ] Solder header pins onto the PN532, if they aren't already on
- [ ] Wire the PN532 to the Pi (VCC→pin 1, GND→pin 6, SDA→pin 3, SCL→pin 5)
- [ ] Check whether the speakers carry audio over USB or only take power (if power only, use the 3.5mm jack)
- [ ] Check the hard drive gets enough power from the Pi; if not, add a powered USB hub
- [ ] Get a reliable 5V 2.5A power supply for the Pi
- [ ] Test how far a card can be from the reader and still be read through the lid/case material

## Pi setup

- [ ] Flash Raspberry Pi OS Lite (Bookworm or later) with SSH and Wi-Fi configured
- [ ] Clone the repo and run `./install.sh`
- [ ] Reboot, then confirm `i2cdetect -y 1` shows the reader at `24`
- [ ] Mount the hard drive at `/mnt/music` via `/etc/fstab` (read-only, `nofail`)
- [ ] Confirm `mpv` plays through the speakers; set `audio_device` in the config if needed
- [ ] Set a comfortable default volume in the config

## First real test

- [ ] Run `musicbox scan` and confirm every card reads
- [ ] Assign a few test tracks with `musicbox assign`
- [ ] Start the service and test: place, remove, swap, unknown card, song ends
- [ ] Tune `misses_to_remove` and `read_timeout` if music cuts out or stops slowly
- [ ] Unplug and replug power; confirm it comes back on its own
- [ ] Boot with the drive unplugged; confirm the Pi still boots and recovers once it's plugged back in

## Music and cards

- [ ] Choose the songs and copy them onto the drive in a tidy folder layout
- [ ] Normalise loudness across tracks so no song is much louder than the others (e.g. ReplayGain or `ffmpeg loudnorm`)
- [ ] Assign every card to its song
- [ ] Label each card (printed sticker with song title, artist and maybe album art)
- [ ] Keep a printed list of cards to songs as a backup
- [ ] Back up `cards.json` somewhere off the Pi

## Enclosure

- [ ] Design or pick a case that holds the Pi, reader, speakers and drive
- [ ] Mark the spot where the card goes clearly (a recess or raised outline)
- [ ] Make sure the reader sits right under that spot with nothing metal in between
- [ ] Add ventilation for the Pi
- [ ] Add strain relief for the power cable so it can't be yanked out of the Pi

## Usability for Dad

- [ ] Play a short confirmation sound when a card is placed, before the song starts
- [ ] Play a different sound for an unknown card or a missing file, so silence isn't the only signal
- [ ] Decide what should happen when a song ends with the card still on: silence (current), repeat, or a sound
- [ ] Decide whether the volume needs to be adjustable by Dad (e.g. a physical knob or "volume up/down" cards)
- [ ] Consider resuming where the song left off if the same card goes back on within a short time
- [ ] Watch Dad use it and fix whatever confuses him

## Reliability and upkeep

- [ ] Enable the read-only overlay filesystem once everything works, to protect the SD card from power cuts
- [ ] Document how to temporarily disable the overlay for updates and new cards
- [ ] Add a way to assign new cards remotely (over SSH, or a small web page on the home network)
- [ ] Make sure the service waits for the drive to be mounted at boot (or handles it appearing later)
- [ ] Keep a spare SD card with a working image

## Software

- [ ] Test the PN532 driver on the real hardware and fix any surprises in `musicbox/reader.py`
- [ ] Add a startup sound when the service is ready
- [ ] Log to a size-limited file or rely on journald limits so logs never fill the SD card
- [ ] Add a `musicbox check` command: reader found, drive mounted, every assigned file exists
- [ ] Set up CI on GitHub to run the tests on each push
