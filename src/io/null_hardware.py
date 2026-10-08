import logging

from .hardware_interface import HardwareIO

logger = logging.getLogger(__name__)


class NullHardwareIO(HardwareIO):
    """No-op hardware backend — used when GPIO is unavailable or not wired.

    Mute is controlled via keyboard: press Enter in the terminal to toggle.
    All LED calls are silently ignored.
    """

    def start(self):
        logger.info("NullHardwareIO active — no GPIO. Press Ctrl+C to exit.")

    def stop(self):
        pass

    def toggle_mute(self):
        self._set_muted(not self._muted)
        logger.info("Mute -> %s", "MUTED" if self._muted else "UNMUTED")

    def set_indicator(self, stage: str):
        pass  # no LED to drive

    def set_warning(self, level: str):
        if level != "off":
            logger.warning("SCAM WARNING LED -> %s", level.upper())
