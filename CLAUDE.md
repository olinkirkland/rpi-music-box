# rpi-music-box

Toniebox-style NFC music player for a Raspberry Pi 3B+ with a PN532 reader
(I2C). See README.md for hardware, setup and behaviour.

## Workflow

- Work directly on `main`: commit and push to `main`. Do not create feature
  branches or pull requests unless asked.

## Development

- Run tests: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest`
- Only `musicbox/reader.py` touches hardware; keep everything else testable off the Pi.
