"""PN532 NFC reader over I2C."""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class Reader:
    """Reads card UIDs. Recovers by reconnecting if the I2C bus hiccups."""

    def __init__(self, timeout: float = 0.2) -> None:
        self.timeout = timeout
        self._pn532 = None

    def _connect(self):
        # Imported here so everything else works off the Pi (tests, dev).
        import board
        import busio
        from adafruit_pn532.i2c import PN532_I2C

        pn532 = PN532_I2C(busio.I2C(board.SCL, board.SDA), debug=False)
        version = pn532.firmware_version
        pn532.SAM_configuration()
        log.info("PN532 connected, firmware %d.%d", version[1], version[2])
        return pn532

    def read(self) -> str | None:
        """UID of the card on the reader as lowercase hex, or None."""
        try:
            if self._pn532 is None:
                self._pn532 = self._connect()
            uid = self._pn532.read_passive_target(timeout=self.timeout)
        except (OSError, RuntimeError, ValueError) as e:
            log.warning("NFC reader error (%s); reconnecting", e)
            self._pn532 = None
            time.sleep(1)
            return None
        return bytes(uid).hex() if uid else None

    def wait_for_card(self) -> str:
        while (uid := self.read()) is None:
            pass
        return uid

    def wait_for_removal(self, misses: int = 3) -> None:
        count = 0
        while count < misses:
            count = count + 1 if self.read() is None else 0
