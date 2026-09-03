"""
Telegram notification layer (Layer 1) - sends exact slot details via the
Telegram Bot API, followed by a short secondary urgent message.
"""
import logging
from datetime import datetime
from typing import Any, Dict

import aiohttp

from app.config import config
from app.safety.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends slot alerts via the Telegram Bot HTTP API (no bot framework needed)."""

    API_BASE = "https://api.telegram.org"

    def __init__(self):
        self._enabled = config.telegram_enabled
        self._token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        rate_limit = config.get('notifications.telegram.rate_limit', 20)
        self._rate_limiter = RateLimiter(max_calls=rate_limit, period_seconds=60)

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram is enabled in config.yaml but bot_token/chat_id are "
                "empty - check .env exists (not .env.txt) and has real values."
            )

    @property
    def is_configured(self) -> bool:
        return bool(self._enabled and self._token and self._chat_id)

    async def _send_message(self, text: str) -> bool:
        if not self.is_configured:
            logger.warning("Telegram not configured - skipping send")
            return False

        await self._rate_limiter.acquire()

        url = f"{self.API_BASE}/bot{self._token}/sendMessage"
        payload = {
            'chat_id': self._chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    logger.error(f"Telegram send failed ({resp.status}): {body}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send raised: {e}")
            return False

    async def send_text(self, text: str) -> bool:
        """Send a plain rate-limited text message (e.g. a human-required notice)."""
        return await self._send_message(text)

    async def send_cycle_summary(self, location_statuses: Dict[str, str]) -> bool:
        """
        Routine per-cycle telemetry - Telegram only (this is a status
        report, not an alert, so no desktop popup or sound). One line per
        configured location plus the exact time the cycle finished.
        """
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = [f"{location}: {status}" for location, status in location_statuses.items()]
        message = (
            "📊 <b>H1B Watcher - Cycle Summary</b>\n"
            f"<b>Time:</b> {timestamp}\n\n" + "\n".join(lines)
        )
        return await self._send_message(message)

    async def send_slot_alert(self, slot: Dict[str, Any], fingerprint: str) -> bool:
        """
        Send the exact slot details, then a short secondary "urgent"
        follow-up so it's hard to miss in a busy chat. Returns True if at
        least one of the two messages went through.
        """
        location = slot.get('location', 'Unknown')
        date = slot.get('date', 'Unknown')
        time_str = slot.get('time') or 'Not specified'
        appt_type = slot.get('appointment_type', 'Interview')
        category = slot.get('category', 'H-1B')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        detail_message = (
            "🎯 <b>H-1B SLOT FOUND</b>\n\n"
            f"<b>Category:</b> {category}\n"
            f"<b>Location:</b> {location}\n"
            f"<b>Type:</b> {appt_type}\n"
            f"<b>Date:</b> {date}\n"
            f"<b>Time:</b> {time_str}\n"
            f"<b>Detected:</b> {timestamp}\n"
            f"<b>Fingerprint:</b> <code>{fingerprint[:16]}...</code>\n\n"
            "⚠️ Automation has PAUSED. Go to the open browser now and book manually."
        )
        ok1 = await self._send_message(detail_message)

        urgent_message = (
            "🚨 <b>URGENT - ACT NOW</b> 🚨\n"
            f"{location} · {date} {time_str} - book before it's gone!"
        )
        ok2 = await self._send_message(urgent_message)

        return ok1 or ok2
