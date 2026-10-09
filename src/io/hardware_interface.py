from abc import ABC, abstractmethod
from typing import Callable, List


class HardwareIO(ABC):
    """Abstract hardware backend. LaptopHardwareIO and PiHardwareIO both
    implement this so orchestrator.py and server.py never check which
    platform they're running on."""

    def __init__(self):
        self._muted = False
        self._mute_listeners: List[Callable[[bool], None]] = []

    @property
    def muted(self) -> bool:
        return self._muted

    def on_mute_change(self, callback: Callable[[bool], None]):
        self._mute_listeners.append(callback)

    def _set_muted(self, value: bool):
        if value == self._muted:
            return
        self._muted = value
        for callback in self._mute_listeners:
            callback(value)

    @abstractmethod
    def start(self):
        """Begin listening for the physical mute control (hotkey or GPIO button)."""

    @abstractmethod
    def stop(self):
        """Release hardware resources."""

    @abstractmethod
    def toggle_mute(self):
        """Flip mute state — called from the physical control and from the API/UI."""

    @abstractmethod
    def set_indicator(self, stage: str):
        """Reflect the current pipeline stage on the visible listening indicator."""
