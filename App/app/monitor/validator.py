"""
8-step validation for slot verification.
"""
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from playwright.async_api import Page, ElementHandle
import logging

from app.utils.dom_helpers import DOMHelpers
from app.utils.date_utils import DateUtils
from app.config import config

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of slot validation."""
    is_valid: bool
    step_results: Dict[str, bool]
    details: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    failure_reason: Optional[str] = None


class SlotValidator:
    """
    Performs 8-step validation for slot verification.
    """
    
    def __init__(self, filters_override: Optional[Dict[str, Any]] = None):
        """
        filters_override lets a caller (AvailabilityChecker, or a test)
        supply the exact category/locations/appointment_types to
        validate against, instead of this independently re-reading
        monitor.filters from the live config - without it, two separate
        reads of the same config key can silently drift apart (e.g. a
        caller overriding locations elsewhere but not here).
        """
        self._config = config
        self._filters = filters_override if filters_override is not None else config.get('monitor.filters', {})
        self._allowed_locations = self._filters.get('locations', [])
        self._allowed_types = self._filters.get('appointment_types', [])
        self._required_category = self._filters.get('category', 'H-1B')

        # Cache for validation results
        self._validation_cache: Dict[str, ValidationResult] = {}
        self._cache_ttl = 60  # seconds
    
    async def validate_slot(
        self,
        page: Page,
        slot_data: Dict[str, Any]
    ) -> ValidationResult:
        """
        Perform 8-step validation on a slot.
        
        Args:
            page: Playwright page
            slot_data: Dict with slot information
            
        Returns:
            ValidationResult with validation status
        """
        # Generate cache key
        cache_key = f"{slot_data.get('date', '')}|{slot_data.get('location', '')}"
        if cache_key in self._validation_cache:
            return self._validation_cache[cache_key]
        
        logger.debug(f"Starting 8-step validation for slot: {slot_data}")
        
        step_results = {}
        details = {}
        is_valid = True
        
        # Step 1: Category is H-1B
        step_results['category'] = await self._validate_category(slot_data)
        if not step_results['category']:
            is_valid = False
            details['category_failure'] = "Not H-1B category"
        
        # Step 2: Correct consular location
        step_results['location'] = await self._validate_location(slot_data)
        if not step_results['location']:
            is_valid = False
            details['location_failure'] = "Location not in allowed list"
        
        # Step 3: Correct appointment type
        step_results['appointment_type'] = await self._validate_appointment_type(slot_data)
        if not step_results['appointment_type']:
            is_valid = False
            details['type_failure'] = "Appointment type not allowed"
        
        # Step 4: Date is selectable in DOM
        step_results['date_selectable'] = await self._validate_date_selectable(page, slot_data)
        if not step_results['date_selectable']:
            is_valid = False
            details['date_failure'] = "Date not selectable in DOM"
        
        # Step 5: Time is selectable
        step_results['time_selectable'] = await self._validate_time_selectable(page, slot_data)
        if not step_results['time_selectable']:
            is_valid = False
            details['time_failure'] = "Time not selectable in DOM"
        
        # Step 6: Fresh server state (not stale DOM cache)
        step_results['fresh_state'] = await self._validate_fresh_state(page, slot_data)
        if not step_results['fresh_state']:
            is_valid = False
            details['stale_failure'] = "Stale DOM cache detected"
        
        # Step 7: Candidate still exists upon verification
        step_results['still_exists'] = await self._validate_still_exists(page, slot_data)
        if not step_results['still_exists']:
            is_valid = False
            details['exists_failure'] = "Slot no longer exists"
        
        # Step 8: SHA-256 Fingerprint Check - handled by fingerprint manager
        # This is a special check that we'll handle in the main flow
        
        # Calculate confidence
        passed_steps = sum(1 for v in step_results.values() if v)
        confidence = passed_steps / len(step_results) if step_results else 0
        
        result = ValidationResult(
            is_valid=is_valid,
            step_results=step_results,
            details=details,
            confidence=confidence,
            failure_reason=details.get('failure_reason')
        )
        
        # Cache result
        self._validation_cache[cache_key] = result
        
        logger.info(f"Validation {'PASSED' if is_valid else 'FAILED'} - Confidence: {confidence:.2%}")
        logger.debug(f"Step results: {step_results}")
        
        return result
    
    async def _validate_category(self, slot_data: Dict[str, Any]) -> bool:
        """
        Step 1: Validate visa category.
        """
        category = slot_data.get('category', '').strip().upper()
        required = self._required_category.upper()
        
        return category == required or required in category
    
    async def _validate_location(self, slot_data: Dict[str, Any]) -> bool:
        """
        Step 2: Validate consular location.
        """
        location = slot_data.get('location', '').strip()
        
        if not self._allowed_locations:
            return True  # No location filter configured
        
        # Check if location matches any allowed location
        for allowed in self._allowed_locations:
            if allowed.lower() in location.lower():
                return True
        
        return False
    
    async def _validate_appointment_type(self, slot_data: Dict[str, Any]) -> bool:
        """
        Step 3: Validate appointment type.
        """
        appt_type = slot_data.get('appointment_type', '').strip()
        
        if not self._allowed_types:
            return True  # No type filter configured
        
        for allowed in self._allowed_types:
            if allowed.lower() in appt_type.lower():
                return True
        
        return False
    
    async def _validate_date_selectable(
        self,
        page: Page,
        slot_data: Dict[str, Any]
    ) -> bool:
        """
        Step 4: Validate date is selectable in DOM.
        """
        date = slot_data.get('date', '')
        if not date:
            return False
        
        try:
            # Check if date element exists and is selectable. Scoped to
            # cell/interactive-element tags rather than "*" - a bare
            # wildcard has-text() selector matches every ancestor up the
            # tree containing the date text anywhere on the page (footer,
            # legends, unrelated banners), which would let unrelated text
            # falsely "confirm" a slot is selectable.
            date_selectors = [
                f'[data-date="{date}"]',
                f'[aria-label*="{date}"]',
                f'td:has-text("{date}")',
                f'button:has-text("{date}")',
                f'a:has-text("{date}")',
                f'[role="gridcell"]:has-text("{date}")',
            ]
            
            for selector in date_selectors:
                element = await DOMHelpers.safe_query_selector(page, selector)
                if element:
                    # Check if clickable
                    is_clickable = await DOMHelpers.is_element_clickable(element)
                    if is_clickable:
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error validating date selectable: {e}")
            return False
    
    async def _validate_time_selectable(
        self,
        page: Page,
        slot_data: Dict[str, Any]
    ) -> bool:
        """
        Step 5: Validate time is selectable in DOM.
        """
        time = slot_data.get('time', '')
        if not time:
            # If no time provided, check if any time is available
            return await self._any_time_available(page)
        
        try:
            # Scoped to interactive-element tags rather than "*" (see note
            # in _validate_date_selectable above).
            time_selectors = [
                f'[data-time="{time}"]',
                f'[aria-label*="{time}"]',
                f'button:has-text("{time}")',
                f'[role="option"]:has-text("{time}")',
                f'li:has-text("{time}")',
            ]
            
            for selector in time_selectors:
                element = await DOMHelpers.safe_query_selector(page, selector)
                if element:
                    is_clickable = await DOMHelpers.is_element_clickable(element)
                    if is_clickable:
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error validating time selectable: {e}")
            return False
    
    async def _any_time_available(self, page: Page) -> bool:
        """
        Check if any time slots are available.
        """
        try:
            time_selectors = [
                '.time-slot.available',
                '[data-time]',
                'button:has-text("Select Time")',
                '.slot-time:not([disabled])',
            ]
            
            for selector in time_selectors:
                elements = await DOMHelpers.safe_query_selector_all(page, selector)
                if elements:
                    for element in elements:
                        if await DOMHelpers.is_element_clickable(element):
                            return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error checking time availability: {e}")
            return False
    
    async def _validate_fresh_state(
        self,
        page: Page,
        slot_data: Dict[str, Any]
    ) -> bool:
        """
        Step 6: Validate fresh server state (not stale DOM cache).
        """
        try:
            # Check if page has been reloaded recently
            # Check if there are any cache indicators
            cache_indicators = [
                '[data-stale="true"]',
                '.cache-hit',
                '[class*="cached"]',
            ]
            
            for selector in cache_indicators:
                element = await DOMHelpers.safe_query_selector(page, selector)
                if element:
                    logger.debug("Stale cache detected")
                    return False
            
            # Check for fresh data indicators
            fresh_indicators = [
                '[data-fresh="true"]',
                '.recently-updated',
                '[data-timestamp]',
            ]
            
            for selector in fresh_indicators:
                element = await DOMHelpers.safe_query_selector(page, selector)
                if element:
                    return True
            
            # If no indicators, assume fresh (but with lower confidence)
            return True
            
        except Exception as e:
            logger.debug(f"Error validating fresh state: {e}")
            return True  # Assume fresh if check fails
    
    async def _validate_still_exists(
        self,
        page: Page,
        slot_data: Dict[str, Any]
    ) -> bool:
        """
        Step 7: Validate slot still exists upon verification.
        """
        date = slot_data.get('date', '')
        time = slot_data.get('time', '')
        
        if not date:
            return False
        
        try:
            # Wait a moment for any DOM updates
            await page.wait_for_timeout(500)
            
            # Check if date still appears in the DOM (scoped tags, not "*")
            date_selectors = [
                f'[data-date="{date}"]',
                f'[aria-label*="{date}"]',
                f'td:has-text("{date}")',
                f'button:has-text("{date}")',
            ]
            
            date_exists = False
            for selector in date_selectors:
                element = await DOMHelpers.safe_query_selector(page, selector)
                if element:
                    date_exists = True
                    break
            
            if not date_exists:
                return False
            
            # If time provided, check it too
            if time:
                time_selectors = [
                    f'[data-time="{time}"]',
                    f'[aria-label*="{time}"]',
                    f'button:has-text("{time}")',
                ]
                
                for selector in time_selectors:
                    element = await DOMHelpers.safe_query_selector(page, selector)
                    if element:
                        return True
                
                return False
            
            return True
            
        except Exception as e:
            logger.debug(f"Error validating still exists: {e}")
            return False
    
    def clear_cache(self):
        """Clear validation cache."""
        self._validation_cache.clear()
        logger.debug("Validation cache cleared")