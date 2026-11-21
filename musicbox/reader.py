"""PN532 NFC reader, over I2C or a USB serial adapter."""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Returned by Reader.read() when the reader itself failed, as opposed to None
# for "no card". A failed read says nothing about whether a card is there.
READ_ERROR = "<read error>"


class Reader:
    """Reads card UIDs. Recovers by reconnecting if the I2C bus hiccups."""

    def __init__(self, timeout: float = 0.2, serial_port: str = "") -> None:
        self.timeout = timeout
        # Empty: the PN532 is wired to the Pi's I2C pins. Otherwise the path of
        # the USB serial adapter it is plugged in through, e.g. /dev/ttyUSB0.
        self.serial_port = serial_port
        self._pn532 = None
        self._serial = None

    def _connect(self):
        # Imported here so everything else works off the Pi (tests, dev).
        if self.serial_port:
            import serial
            from adafruit_pn532.uart import PN532_UART

            self._close_serial()
            self._serial = serial.Serial(self.serial_port, 115200, timeout=0.1)
            pn532 = PN532_UART(self._serial, debug=False)
            how = self.serial_port
        else:
            import board
            import busio
            from adafruit_pn532.i2c import PN532_I2C

            pn532 = PN532_I2C(busio.I2C(board.SCL, board.SDA), debug=False)
            how = "I2C"
        version = pn532.firmware_version
        pn532.SAM_configuration()
        log.info("PN532 connected on %s, firmware %d.%d", how, version[1], version[2])
        return pn532

    def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except OSError:
                pass
            self._serial = None

    def read(self) -> str | None:
        """UID of the card on the reader as lowercase hex, None for no card,
        or READ_ERROR if the reader failed."""
        try:
            if self._pn532 is None:
                self._pn532 = self._connect()
            uid = self._pn532.read_passive_target(timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - any driver/serial failure means reconnect
            log.warning("NFC reader error (%s); reconnecting", e)
            self._pn532 = None
            self._close_serial()
            time.sleep(1)
            return READ_ERROR
        return bytes(uid).hex() if uid else None

    def wait_for_card(self) -> str:
        while (uid := self.read()) in (None, READ_ERROR):
            pass
        return uid

    def wait_for_removal(self, misses: int = 3) -> None:
        count = 0
        while count < misses:
            uid = self.read()
            if uid is None:
                count += 1
            elif uid != READ_ERROR:
                count = 0
