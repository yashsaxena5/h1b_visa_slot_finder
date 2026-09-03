"""
Native desktop notification layer (Layer 2).
"""
import asyncio
import logging
import platform
import shutil

from app.config import config

logger = logging.getLogger(__name__)


class DesktopNotifier:
    """Fires a native OS popup. macOS via osascript, Linux via notify-send."""

    def __init__(self):
        self._enabled = config.get('notifications.desktop.enabled', True)
        self._system = platform.system()  # 'Darwin', 'Linux', 'Windows'

    async def notify(self, title: str, message: str) -> bool:
        if not self._enabled:
            return False

        try:
            if self._system == 'Darwin':
                return await self._notify_macos(title, message)
            elif self._system == 'Linux':
                return await self._notify_linux(title, message)
            else:
                logger.warning(
                    f"Desktop notifications not implemented for {self._system}; "
                    f"would have shown: {title} - {message}"
                )
                return False
        except Exception as e:
            logger.error(f"Desktop notification failed: {e}")
            return False

    @staticmethod
    def _escape_applescript(text: str) -> str:
        return text.replace('\\', '\\\\').replace('"', '\\"')

    async def _notify_macos(self, title: str, message: str) -> bool:
        script = (
            f'display notification "{self._escape_applescript(message)}" '
            f'with title "{self._escape_applescript(title)}" sound name "Glass"'
        )
        proc = await asyncio.create_subprocess_exec(
            'osascript', '-e', script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"osascript notification failed: {stderr.decode(errors='ignore')}")
            return False
        return True

    async def _notify_linux(self, title: str, message: str) -> bool:
        if not shutil.which('notify-send'):
            logger.warning("notify-send not found - install libnotify-bin for desktop notifications")
            return False
        proc = await asyncio.create_subprocess_exec(
            'notify-send', '--urgency=critical', title, message,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"notify-send failed: {stderr.decode(errors='ignore')}")
            return False
        return True
