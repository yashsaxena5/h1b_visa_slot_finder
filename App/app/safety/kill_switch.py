"""
Global emergency kill switch: a background hotkey listener (Ctrl+Shift+Q
by default) that requests an immediate clean stop. This is in addition to
the always-available config.yaml `app.enabled: false` switch, which is
checked automatically by Config.enabled (see app/config.py) - the two
are independent, so either one alone is enough to stop the machine.
"""
import asyncio
import logging
from typing import Optional

from app.config import config

logger = logging.getLogger(__name__)


class KillSwitch:
    """
    Wraps pynput's GlobalHotKeys (which runs its own OS-level listener
    thread) and exposes an asyncio.Event that the state machine's run
    loop polls. Hotkey callbacks fire on pynput's thread, not the asyncio
    loop, so they hop back over via call_soon_threadsafe.
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        self._loop = loop or asyncio.get_event_loop()
        self.triggered = asyncio.Event()
        self._hotkey_str = config.get('safety.kill_switch_hotkey', '<ctrl>+<shift>+q')
        self._listener = None

    def _on_activate(self):
        logger.critical(f"🛑 Kill switch hotkey ({self._hotkey_str}) pressed - stopping.")
        self._loop.call_soon_threadsafe(self.triggered.set)

    def start(self):
        """
        Arm the global hotkey listener. Safe to call even if it fails
        (e.g. macOS Accessibility permission not granted for this
        terminal/process) - the config.yaml switch still works either way.
        """
        try:
            from pynput import keyboard
            self._listener = keyboard.GlobalHotKeys({self._hotkey_str: self._on_activate})
            self._listener.start()
            logger.info(f"Kill switch armed: press {self._hotkey_str} to stop immediately.")
        except Exception as e:
            logger.warning(
                f"Could not start the global hotkey listener ({e}). On macOS this "
                f"usually means Accessibility permission hasn't been granted to "
                f"the terminal/app running this process. The config.yaml kill "
                f"switch (set app.enabled: false) still works regardless."
            )

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None
