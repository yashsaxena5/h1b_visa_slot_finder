"""
Maintenance mode detection and handling.
"""
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, time, timedelta
from playwright.async_api import Page
import logging
import re

logger = logging.getLogger(__name__)


class MaintenanceDetector:
    """
    Detects and handles portal maintenance mode.
    """
    
    def __init__(self):
        # Known maintenance windows (Eastern Time)
        # CGI Federal typically does maintenance Sunday 12AM-6AM EST
        self._known_windows = [
            {
                'day': 6,  # Sunday
                'start': time(0, 0),
                'end': time(6, 0),
                'description': "Sunday 12AM-6AM EST"
            }
        ]
        
        self._is_maintenance = False
        self._maintenance_start: Optional[datetime] = None
        self._maintenance_end: Optional[datetime] = None
        self._detection_patterns = [
            r'maintenance',
            r'under\s+construction',
            r'system\s+maintenance',
            r'scheduled\s+maintenance',
            r'down\s+for\s+maintenance',
            r'please\s+try\s+again\s+later',
            r'service\s+unavailable',
            r'temporarily\s+unavailable',
        ]
    
    async def check_maintenance(self, page: Page) -> Dict[str, Any]:
        """
        Check if portal is in maintenance mode.
        
        Returns:
            Dict with maintenance status and details
        """
        try:
            # Check content for maintenance messages
            content = await page.content()
            title = await page.title()
            
            # Check patterns
            is_maintenance, details = self._detect_maintenance_text(content, title)
            
            # Check for maintenance elements
            if not is_maintenance:
                is_maintenance, details = await self._detect_maintenance_elements(page)
            
            # Check known maintenance windows
            if not is_maintenance:
                is_maintenance, details = self._check_known_window()
            
            # Update state
            if is_maintenance and not self._is_maintenance:
                self._maintenance_start = datetime.now()
                self._maintenance_end = self._estimate_end_time(details)
                logger.warning(f"Maintenance detected: {details}")
            
            self._is_maintenance = is_maintenance
            
            return {
                'is_maintenance': is_maintenance,
                'start_time': self._maintenance_start,
                'end_time': self._maintenance_end,
                'details': details,
                'detected_by': 'text' if is_maintenance else 'none',
            }
            
        except Exception as e:
            logger.error(f"Error checking maintenance: {e}")
            return {
                'is_maintenance': False,
                'details': f"Error checking: {e}",
                'detected_by': 'error',
            }
    
    def _detect_maintenance_text(self, content: str, title: str) -> Tuple[bool, str]:
        """Detect maintenance from page content."""
        combined = f"{content} {title}".lower()
        
        for pattern in self._detection_patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                # Try to extract time information
                time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', content)
                time_info = f" until {time_match.group(1)}" if time_match else ""
                return True, f"Maintenance message found{time_info}"
        
        return False, ""
    
    async def _detect_maintenance_elements(self, page: Page) -> Tuple[bool, str]:
        """Detect maintenance from DOM elements."""
        maintenance_selectors = [
            '.maintenance',
            '.maintenance-banner',
            '.system-maintenance',
            '.alert-maintenance',
            '[class*="maintenance"]',
            '[class*="under-construction"]',
        ]
        
        for selector in maintenance_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    text = await element.text_content()
                    return True, f"Maintenance element found: {text[:50] if text else selector}"
            except:
                pass
        
        return False, ""
    
    def _check_known_window(self) -> Tuple[bool, str]:
        """Check if current time falls in known maintenance window."""
        # Note: This uses local time, should use Eastern Time in production
        now = datetime.now()
        current_day = now.weekday()
        current_time = now.time()
        
        for window in self._known_windows:
            if current_day == window['day']:
                if window['start'] <= current_time <= window['end']:
                    return True, f"Scheduled maintenance: {window['description']}"
        
        return False, ""
    
    def _estimate_end_time(self, details: str) -> Optional[datetime]:
        """Estimate when maintenance will end."""
        # Try to extract time from details
        time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', details)
        if time_match:
            try:
                time_str = time_match.group(1)
                # Simple parsing - in production use dateparser
                return datetime.now() + timedelta(hours=1)  # Fallback
            except:
                pass
        
        # If in known window, use window end time
        now = datetime.now()
        for window in self._known_windows:
            if now.weekday() == window['day']:
                end_time = datetime.combine(now.date(), window['end'])
                if now.time() < window['end']:
                    return end_time
        
        # Default: 1 hour from now
        return datetime.now() + timedelta(hours=1)
    
    def get_estimated_remaining_time(self) -> Optional[timedelta]:
        """Get estimated remaining time until maintenance ends."""
        if not self._is_maintenance or not self._maintenance_end:
            return None
        
        remaining = self._maintenance_end - datetime.now()
        return max(remaining, timedelta(0))
    
    def clear_maintenance(self):
        """Clear maintenance state (call when portal is back)."""
        if self._is_maintenance:
            logger.info("Maintenance cleared")
            self._is_maintenance = False
            self._maintenance_start = None
            self._maintenance_end = None
    
    def is_maintenance(self) -> bool:
        """Check if currently in maintenance."""
        return self._is_maintenance