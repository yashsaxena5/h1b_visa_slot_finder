"""
Specialized session expiry detection and handling.
"""
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from playwright.async_api import Page
import logging

from .state_detector import StateDetector, PortalState

logger = logging.getLogger(__name__)


class SessionExpiryDetector:
    """
    Detects and handles session expiry with multiple methods.
    """
    
    def __init__(self):
        self._state_detector = StateDetector()
        self._session_health: Dict[str, Any] = {
            'is_valid': False,
            'last_check': None,
            'check_count': 0,
            'consecutive_valid': 0,
            'consecutive_invalid': 0,
        }
        
        # Session expiration patterns (in addition to state detector)
        self._expiry_patterns = [
            r'session.*expired',
            r'log.*in.*again',
            r'sign.*in.*continue',
            r'session.*timeout',
            r'your session has (?:expired|ended)',
            r'please login',
            r'authentication required',
        ]
    
    async def check_session(self, page: Page) -> Dict[str, Any]:
        """
        Check if session is still valid using multiple methods.
        
        Args:
            page: Playwright page
            
        Returns:
            Dict with session status
        """
        self._session_health['check_count'] += 1
        self._session_health['last_check'] = datetime.now()
        
        # Method 1: Use state detector
        state_result = await self._state_detector.detect(page, force_refresh=True)
        
        # Method 2: Check for specific elements
        has_login_form = await self._check_login_form(page)
        
        # Method 3: Check URL patterns
        url_pattern_expired = await self._check_url_pattern(page)
        
        # Method 4: Check for session expiry messages
        has_expiry_message = await self._check_expiry_messages(page)
        
        # Determine session validity
        is_valid = self._determine_validity(
            state_result,
            has_login_form,
            url_pattern_expired,
            has_expiry_message
        )
        
        # Update health tracking
        if is_valid:
            self._session_health['consecutive_valid'] += 1
            self._session_health['consecutive_invalid'] = 0
        else:
            self._session_health['consecutive_valid'] = 0
            self._session_health['consecutive_invalid'] += 1
        
        self._session_health['is_valid'] = is_valid
        
        # Log session status
        status = "VALID" if is_valid else "INVALID"
        logger.debug(f"Session check #{self._session_health['check_count']}: {status}")
        
        return {
            'is_valid': is_valid,
            'state': state_result.state.value,
            'state_confidence': state_result.confidence,
            'has_login_form': has_login_form,
            'url_expired': url_pattern_expired,
            'has_expiry_message': has_expiry_message,
            'health': self._session_health,
        }
    
    async def _check_login_form(self, page: Page) -> bool:
        """Check for login form elements."""
        try:
            # Check for username/email and password fields
            username_fields = await page.query_selector_all(
                'input[type="email"], input[name*="username"], input[name*="userid"], input[name*="email"]'
            )
            password_fields = await page.query_selector_all(
                'input[type="password"]'
            )
            
            # Check for login button
            login_buttons = await page.query_selector_all(
                'button[type="submit"], button:has-text("Login"), button:has-text("Sign In")'
            )
            
            # If we have both username and password fields, it's likely a login form
            if len(username_fields) > 0 and len(password_fields) > 0:
                return True
            
            # If we have a login button with fields
            if len(login_buttons) > 0 and (len(username_fields) > 0 or len(password_fields) > 0):
                return True
                
        except Exception as e:
            logger.debug(f"Error checking login form: {e}")
        
        return False
    
    async def _check_url_pattern(self, page: Page) -> bool:
        """Check if URL indicates session expiry."""
        url = page.url.lower()
        expired_indicators = ['login', 'signin', 'auth', 'session']
        return any(indicator in url for indicator in expired_indicators)
    
    async def _check_expiry_messages(self, page: Page) -> bool:
        """Check for session expiry messages in content."""
        try:
            content = await page.content()
            content_lower = content.lower()
            
            import re
            for pattern in self._expiry_patterns:
                if re.search(pattern, content_lower, re.IGNORECASE):
                    return True
                    
        except Exception as e:
            logger.debug(f"Error checking expiry messages: {e}")
        
        return False
    
    def _determine_validity(
        self,
        state_result,
        has_login_form: bool,
        url_pattern_expired: bool,
        has_expiry_message: bool
    ) -> bool:
        """Determine session validity based on all signals."""
        # If state detector says session expired, trust it
        if state_result.state == PortalState.SESSION_EXPIRED:
            return False
        
        # If state detector says login required
        if state_result.state == PortalState.LOGIN_REQUIRED:
            return False
        
        # If we have login form, definitely invalid
        if has_login_form:
            return False
        
        # If URL indicates login/session
        if url_pattern_expired:
            return False
        
        # If expiry message found
        if has_expiry_message:
            return False
        
        # CAPTCHA/access-denied/maintenance aren't session *expiry*, but the
        # session is not safely usable while any of them are showing - the
        # caller must not treat this as "go ahead and navigate/interact".
        if state_result.state in [
            PortalState.CAPTCHA,
            PortalState.ACCESS_DENIED,
            PortalState.MAINTENANCE
        ]:
            return False

        # Default to valid if no signals indicate otherwise
        return True
    
    def is_session_healthy(self) -> bool:
        """Check if session health is good based on history."""
        return (
            self._session_health['is_valid'] and
            self._session_health['consecutive_valid'] >= 1 and
            self._session_health['consecutive_invalid'] == 0
        )
    
    def get_session_age(self) -> Optional[timedelta]:
        """Get age of current session."""
        if self._session_health['last_check']:
            return datetime.now() - self._session_health['last_check']
        return None
    
    def get_session_summary(self) -> str:
        """Get a human-readable session summary."""
        if not self._session_health['last_check']:
            return "No session checks performed"
        
        status = "✅ Healthy" if self.is_session_healthy() else "❌ Unhealthy"
        checks = self._session_health['check_count']
        valid_ratio = self._session_health['consecutive_valid'] / max(checks, 1)
        
        return (
            f"Session Status: {status}\n"
            f"Checks: {checks}\n"
            f"Valid Ratio: {valid_ratio:.2%}\n"
            f"Last Check: {self._session_health['last_check'].strftime('%H:%M:%S')}"
        )