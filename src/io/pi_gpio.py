import logging
import threading
import time

import RPi.GPIO as GPIO

from .hardware_interface import HardwareIO

logger = logging.getLogger(__name__)

_SWITCH_PIN = 17   # pin 11 — on/off switch
_GREEN_PIN  = 22   # pin 15 — listening (switch ON)
_RED_PIN    = 27   # pin 13 — muted    (switch OFF)


class PiHardwareIO(HardwareIO):
    """
    Switch ON  (closed to GND) → green LED on,  red LED off  → listening
    Switch OFF (open)          → red LED on,  green LED off → muted
    """

    def __init__(self, mute_button_pin: int = _SWITCH_PIN, led_pin: int = _GREEN_PIN):
        super().__init__()
        self._mute_pin  = mute_button_pin
        self._green_pin = led_pin
        self._red_pin   = _RED_PIN
        self._poll_stop = threading.Event()
        self._last_state = None

    def start(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self._mute_pin,  GPIO.IN,  pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self._green_pin, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self._red_pin,   GPIO.OUT, initial=GPIO.LOW)

        initial_muted = GPIO.input(self._mute_pin) == GPIO.HIGH
        self._last_state = GPIO.input(self._mute_pin)
        self._set_muted(initial_muted)
        self._apply_leds(initial_muted)

        threading.Thread(target=self._poll_loop, daemon=True, name="gpio-poll").start()
        logger.info("GPIO ready — switch=GPIO%d (boot=%s) green=GPIO%d red=GPIO%d",
                    self._mute_pin, "MUTED" if initial_muted else "ACTIVE",
                    self._green_pin, self._red_pin)

    def _apply_leds(self, muted: bool):
        GPIO.output(self._green_pin, GPIO.LOW  if muted else GPIO.HIGH)
        GPIO.output(self._red_pin,   GPIO.HIGH if muted else GPIO.LOW)

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            current = GPIO.input(self._mute_pin)
            if current != self._last_state:
                time.sleep(0.05)
                if GPIO.input(self._mute_pin) == current:
                    self._last_state = current
                    muted = current == GPIO.HIGH
                    self._set_muted(muted)
                    self._apply_leds(muted)
                    logger.info("Switch → %s", "MUTED" if muted else "ACTIVE")
            self._poll_stop.wait(0.05)

    def stop(self):
        self._poll_stop.set()
        GPIO.output(self._green_pin, GPIO.LOW)
        GPIO.output(self._red_pin,   GPIO.LOW)
        GPIO.cleanup()

    def toggle_mute(self):
        new_muted = not self._muted
        self._set_muted(new_muted)
        self._apply_leds(new_muted)

    def set_indicator(self, stage: str):
        self._apply_leds(self._muted or stage == "muted")
