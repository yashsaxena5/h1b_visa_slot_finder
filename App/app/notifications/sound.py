"""
Continuous audio alarm layer (Layer 3) - loud, hard to miss, repeats on a
loop until acknowledged (stopped) or a bounded max duration is reached.
"""
import asyncio
import logging
import platform
import shutil
from pathlib import Path
from typing import Optional

from app.config import config

logger = logging.getLogger(__name__)

# Ships with macOS - lets the alarm work out of the box with no extra asset.
_MACOS_FALLBACK_SOUND = "/System/Library/Sounds/Sosumi.aiff"


class SoundAlarm:
    """
    Plays an alarm sound on a loop every `repeat_interval` seconds, for up
    to `max_repeats` repeats, or until `.stop()` is called.

    The repeat count is intentionally bounded (default ~2 minutes worth):
    an alarm with no human nearby to acknowledge it should not literally
    beep forever. Telegram and the desktop popup remain the durable
    record after the alarm auto-stops.
    """

    def __init__(self):
        self._enabled = config.get('notifications.sound.enabled', True)
        self._sound_file = config.get('notifications.sound.sound_file', './assets/alarm.mp3')
        self._repeat_interval = config.get('notifications.sound.repeat_interval', 5)
        self._max_repeats = config.get('notifications.sound.max_repeats', 24)
        self._system = platform.system()
        self._task: Optional[asyncio.Task] = None

    def _resolve_sound_path(self) -> Optional[str]:
        path = Path(self._sound_file)
        if path.exists():
            return str(path)

        if self._system == 'Darwin' and Path(_MACOS_FALLBACK_SOUND).exists():
            logger.warning(
                f"{self._sound_file} not found - using a built-in macOS alert "
                f"sound instead. Add your own file at {self._sound_file} to customize."
            )
            return _MACOS_FALLBACK_SOUND

        logger.warning(f"No alarm sound file found at {self._sound_file} and no fallback available")
        return None

    async def _play_once(self, sound_path: Optional[str]) -> None:
        try:
            if sound_path and self._system == 'Darwin':
                proc = await asyncio.create_subprocess_exec(
                    'afplay', sound_path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await proc.communicate()
                return

            if sound_path and self._system == 'Linux':
                player = shutil.which('paplay') or shutil.which('aplay')
                if player:
                    proc = await asyncio.create_subprocess_exec(
                        player, sound_path,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                    )
                    await proc.communicate()
                    return

            if sound_path and self._system == 'Windows':
                import winsound
                winsound.PlaySound(sound_path, winsound.SND_FILENAME)
                return

        except Exception as e:
            logger.error(f"Failed to play alarm sound: {e}")

        # Fallback: terminal bell (works everywhere, needs no dependency)
        print('\a', end='', flush=True)

    async def _loop(self):
        sound_path = self._resolve_sound_path()
        repeats = 0
        try:
            while repeats < self._max_repeats:
                await self._play_once(sound_path)
                repeats += 1
                await asyncio.sleep(self._repeat_interval)
            logger.info(
                "Alarm reached its max repeat count and stopped automatically; "
                "Telegram/desktop alerts remain the durable record."
            )
        except asyncio.CancelledError:
            logger.debug("Alarm stopped (acknowledged)")
            raise

    def start(self):
        """Start the alarm loop in the background. No-op if disabled or already running."""
        if not self._enabled:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        """Stop the alarm immediately - an explicit human acknowledgement."""
        if self._task and not self._task.done():
            self._task.cancel()
