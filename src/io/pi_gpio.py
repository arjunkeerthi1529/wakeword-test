import logging

from .hardware_interface import HardwareIO

logger = logging.getLogger(__name__)


class PiHardwareIO(HardwareIO):
    """Raspberry Pi GPIO backend using gpiozero.

    Mute button   — GPIO 17, pulled high, active low, 100ms debounce.
    Listening LED — GPIO 27:
                      fast blink (0.1s/0.1s) while capturing speech,
                      slow blink (0.5s/0.5s) while LLM is thinking,
                      solid on while TTS is playing.
    Online LED    — GPIO 22: solid on during any network call (currently unused
                      in this project but wired for hardware compatibility).

    Stages passed to set_indicator():
        "waiting_for_wake"  — LED off (idle, gate closed)
        "listening"         — fast blink (capturing user speech)
        "thinking"          — slow blink (STT + LLM processing)
        "speaking"          — solid on (TTS playback)
        "muted"             — LED off
    """

    def __init__(
        self,
        mute_button_pin: int = 17,
        listening_led_pin: int = 27,
        online_led_pin: int = 22,
    ):
        super().__init__()
        from gpiozero import Button, LED  # imported here so non-Pi machines can import the module
        self._button = Button(mute_button_pin, pull_up=True, bounce_time=0.1)
        self._listen_led = LED(listening_led_pin)
        self._online_led = LED(online_led_pin)
        logger.info(
            "PiHardwareIO initialised — mute_btn=GPIO%d  listen_led=GPIO%d  online_led=GPIO%d",
            mute_button_pin,
            listening_led_pin,
            online_led_pin,
        )

    def start(self):
        """Wire the mute button callback. Call once after __init__."""
        self._button.when_pressed = self.toggle_mute
        logger.info(
            "GPIO started — press button on GPIO%d to mute/unmute",
            self._button.pin.number,
        )

    def stop(self):
        """Turn off LEDs and release all GPIO resources."""
        self._listen_led.off()
        self._online_led.off()
        self._button.close()
        self._listen_led.close()
        self._online_led.close()
        logger.info("GPIO released")

    def toggle_mute(self):
        """Called by gpiozero on button press."""
        self._set_muted(not self._muted)
        logger.info("Mute toggled -> %s", "MUTED" if self._muted else "UNMUTED")
        if self._muted:
            self._listen_led.off()

    def set_indicator(self, stage: str):
        """Drive the listening LED to reflect the current pipeline stage."""
        if self._muted:
            self._listen_led.off()
            return
        if stage == "listening":
            self._listen_led.blink(on_time=0.1, off_time=0.1)
        elif stage == "thinking":
            self._listen_led.blink(on_time=0.5, off_time=0.5)
        elif stage == "speaking":
            self._listen_led.on()
        else:  # "waiting_for_wake", "muted", unknown
            self._listen_led.off()

    def set_warning(self, level: str):
        """Scam-warning LED on GPIO22: solid for warn, slow blink for watch."""
        if level == "warn":
            self._online_led.on()
        elif level == "watch":
            self._online_led.blink(on_time=0.5, off_time=0.5)
        else:
            self._online_led.off()
