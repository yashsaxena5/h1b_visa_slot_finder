"""
Fans a confirmed slot alert out across all three notification layers.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import config
from app.notifications.telegram import TelegramNotifier
from app.notifications.desktop import DesktopNotifier
from app.notifications.sound import SoundAlarm

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Layer 1 (Telegram) + Layer 2 (desktop popup) + Layer 3 (sound alarm).

    Each layer is isolated: a failure in one must not prevent the others
    from firing - the whole point of three layers is redundancy.
    """

    def __init__(self):
        self._telegram = TelegramNotifier()
        self._desktop = DesktopNotifier()
        self._sound = SoundAlarm()

        # Routine per-cycle telemetry (see notify_cycle_summary) is
        # throttled independently of the check-cycle interval itself,
        # which can be as low as 30s - without this, an unattended run
        # would turn "know what's happening without watching the screen"
        # into a Telegram spam stream instead. 0 disables it entirely.
        interval_minutes = config.get('notifications.telegram.cycle_summary_interval_minutes', 30)
        self._cycle_summary_interval: Optional[timedelta] = (
            timedelta(minutes=interval_minutes) if interval_minutes > 0 else None
        )
        self._last_cycle_summary_at: Optional[datetime] = None

    async def send_slot_alert(self, slot: Dict[str, Any], fingerprint: str) -> Dict[str, bool]:
        results = {'telegram': False, 'desktop': False, 'sound': False}

        try:
            results['telegram'] = await self._telegram.send_slot_alert(slot, fingerprint)
        except Exception as e:
            logger.error(f"Telegram layer failed: {e}")

        try:
            title = "H-1B Slot Found!"
            message = f"{slot.get('location', 'Unknown')} - {slot.get('date', '')} {slot.get('time', '')}".strip()
            results['desktop'] = await self._desktop.notify(title, message)
        except Exception as e:
            logger.error(f"Desktop layer failed: {e}")

        try:
            self._sound.start()
            results['sound'] = True
        except Exception as e:
            logger.error(f"Sound layer failed: {e}")

        logger.critical(f"🔔 ALERT DISPATCHED for slot {slot} - results: {results}")
        return results

    async def notify_human_required(self, reason: str) -> Dict[str, bool]:
        """
        Lighter-weight notice for non-slot halts (session expiry, CAPTCHA,
        unrecoverable errors) - Telegram + desktop only. No alarm: that's
        reserved for confirmed slots so it doesn't lose urgency from
        being triggered on every session hiccup.
        """
        results = {'telegram': False, 'desktop': False}
        message = f"⏸️ H1B Slot Watcher has PAUSED and needs you.\nReason: {reason}"

        try:
            results['telegram'] = await self._telegram.send_text(message)
        except Exception as e:
            logger.error(f"Telegram human-required notice failed: {e}")

        try:
            results['desktop'] = await self._desktop.notify("H1B Watcher Needs You", reason)
        except Exception as e:
            logger.error(f"Desktop human-required notice failed: {e}")

        return results

    async def notify_cycle_summary(self, location_statuses: Dict[str, str]) -> bool:
        """
        Routine telemetry after a check cycle finishes: exact timestamp
        plus each configured location's status ('No slots' / 'Glitched' /
        'Slots Found'). Telegram only - this is a status report, not an
        alert.

        Throttled to at most once per cycle_summary_interval_minutes
        (config, default 30) regardless of how often the caller invokes
        this - the underlying poll interval can be much shorter than
        that, and this is meant to be a periodic heartbeat, not a message
        per check.
        """
        if not location_statuses or self._cycle_summary_interval is None:
            return False

        now = datetime.now()
        if (self._last_cycle_summary_at is not None
                and now - self._last_cycle_summary_at < self._cycle_summary_interval):
            return False

        try:
            sent = await self._telegram.send_cycle_summary(location_statuses)
        except Exception as e:
            logger.error(f"Cycle summary notification failed: {e}")
            return False

        if sent:
            self._last_cycle_summary_at = now
        return sent

    def acknowledge(self):
        """Stop any active alarm - call when the human takes over."""
        self._sound.stop()
