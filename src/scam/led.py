"""Optional warning LED. Never fatal: without GPIO it just logs."""
import logging

logger = logging.getLogger(__name__)


class WarningLED:
    def __init__(self, pin: int = 22):
        self._led = None
        if not pin:
            return
        try:
            from gpiozero import LED
            self._led = LED(pin)
            logger.info("Warning LED on GPIO%d", pin)
        except Exception as exc:
            logger.warning("Warning LED unavailable (%s) — warnings will only be logged", exc)

    def set(self, level: str) -> None:
        """level: "warn" (solid), "watch" (slow blink), or "off"."""
        if level != "off":
            logger.warning("SCAM WARNING LED -> %s", level.upper())
        if self._led is None:
            return
        try:
            if level == "warn":
                self._led.on()
            elif level == "watch":
                self._led.blink(on_time=0.5, off_time=0.5)
            else:
                self._led.off()
        except Exception:
            logger.exception("LED error")
