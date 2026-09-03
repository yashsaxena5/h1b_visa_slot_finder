"""
Safe DOM operations with anti-detection measures.
"""
from typing import Optional, List, Dict, Any, Tuple
from playwright.async_api import Page, ElementHandle
import logging
import re
import random

logger = logging.getLogger(__name__)


class DOMHelpers:
    """
    Helper functions for DOM operations with safety measures.
    """
    
    @staticmethod
    async def safe_query_selector(
        page: Page, 
        selector: str, 
        timeout: int = 5000
    ) -> Optional[ElementHandle]:
        """
        Safely query selector with timeout and error handling.
        """
        try:
            element = await page.query_selector(selector)
            return element
        except Exception as e:
            logger.debug(f"Safe query failed for {selector}: {e}")
            return None
    
    @staticmethod
    async def safe_query_selector_all(
        page: Page, 
        selector: str
    ) -> List[ElementHandle]:
        """
        Safely query all selectors.
        """
        try:
            elements = await page.query_selector_all(selector)
            return elements
        except Exception as e:
            logger.debug(f"Safe query all failed for {selector}: {e}")
            return []
    
    @staticmethod
    async def get_element_text_safe(
        element: ElementHandle
    ) -> Optional[str]:
        """
        Safely get text content from element.
        """
        try:
            text = await element.text_content()
            return text.strip() if text else None
        except Exception as e:
            logger.debug(f"Failed to get text: {e}")
            return None
    
    @staticmethod
    async def get_element_attribute_safe(
        element: ElementHandle,
        attribute: str
    ) -> Optional[str]:
        """
        Safely get attribute from element.
        """
        try:
            return await element.get_attribute(attribute)
        except Exception as e:
            logger.debug(f"Failed to get attribute {attribute}: {e}")
            return None
    
    @staticmethod
    async def get_date_picker_data(
        page: Page
    ) -> List[Dict[str, Any]]:
        """
        Get available dates from date picker with safe extraction.
        """
        available_dates = []
        
        try:
            # Common selectors for date pickers on CGI Federal
            date_selectors = [
                'td.available, td.ui-datepicker-available',
                '.slot-available, .date-available',
                'td[data-available="true"]',
                '.available-slot, .slot-open',
                'button:has-text("Available")',
                'div[class*="available"]:has-text("Select")',
            ]
            
            for selector in date_selectors:
                elements = await DOMHelpers.safe_query_selector_all(page, selector)
                if elements:
                    for element in elements:
                        date_info = await DOMHelpers._extract_date_from_element(element)
                        if date_info:
                            available_dates.append(date_info)
            
            # If no dates found with specific selectors, try generic approach
            if not available_dates:
                available_dates = await DOMHelpers._extract_dates_generic(page)
            
        except Exception as e:
            logger.error(f"Error extracting date picker data: {e}")
        
        return available_dates
    
    @staticmethod
    async def _extract_date_from_element(
        element: ElementHandle
    ) -> Optional[Dict[str, Any]]:
        """
        Extract date information from a DOM element.
        """
        try:
            # Try data attributes first
            date_str = await DOMHelpers.get_element_attribute_safe(element, 'data-date')
            if date_str:
                return {
                    'date': date_str,
                    'element': element,
                    'confidence': 0.9,
                    'source': 'data-attribute'
                }
            
            # Try aria-label
            aria_label = await DOMHelpers.get_element_attribute_safe(element, 'aria-label')
            if aria_label and 'date' in aria_label.lower():
                date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})', aria_label)
                if date_match:
                    return {
                        'date': date_match.group(1),
                        'element': element,
                        'confidence': 0.8,
                        'source': 'aria-label'
                    }
            
            # Try text content
            text = await DOMHelpers.get_element_text_safe(element)
            if text:
                # Look for date patterns
                date_match = re.search(
                    r'(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-z]+\s+\d{1,2},\s+\d{4})',
                    text
                )
                if date_match:
                    return {
                        'date': date_match.group(1),
                        'element': element,
                        'confidence': 0.7,
                        'source': 'text-content'
                    }
            
            return None
            
        except Exception as e:
            logger.debug(f"Error extracting date from element: {e}")
            return None
    
    @staticmethod
    async def _extract_dates_generic(
        page: Page
    ) -> List[Dict[str, Any]]:
        """
        Generic extraction of dates from page content.
        """
        dates = []
        
        try:
            # Get all text content
            content = await page.content()
            
            # Find all date patterns
            date_patterns = [
                r'(\d{1,2}/\d{1,2}/\d{4})',  # MM/DD/YYYY or DD/MM/YYYY
                r'(\d{4}-\d{2}-\d{2})',       # YYYY-MM-DD
                r'([A-Za-z]+\s+\d{1,2},\s+\d{4})',  # Month DD, YYYY
            ]
            
            for pattern in date_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    dates.append({
                        'date': match,
                        'element': None,
                        'confidence': 0.5,
                        'source': 'generic-pattern'
                    })
            
            # Remove duplicates while preserving order
            seen = set()
            unique_dates = []
            for d in dates:
                if d['date'] not in seen:
                    seen.add(d['date'])
                    unique_dates.append(d)
            
            return unique_dates
            
        except Exception as e:
            logger.error(f"Error in generic date extraction: {e}")
            return []
    
    @staticmethod
    async def is_element_clickable(
        element: ElementHandle
    ) -> bool:
        """
        Check if element appears to be clickable.
        """
        try:
            # Check disabled attribute
            disabled = await DOMHelpers.get_element_attribute_safe(element, 'disabled')
            if disabled is not None:
                return False
            
            # Check for pointer-events none
            style = await DOMHelpers.get_element_attribute_safe(element, 'style')
            if style and 'pointer-events: none' in style:
                return False
            
            # Check for aria-disabled
            aria_disabled = await DOMHelpers.get_element_attribute_safe(element, 'aria-disabled')
            if aria_disabled == 'true':
                return False
            
            return True
            
        except Exception as e:
            logger.debug(f"Error checking clickable: {e}")
            return False
    
    @staticmethod
    async def get_current_url(page: Page) -> str:
        """
        Safely get current URL.
        """
        try:
            return page.url
        except Exception as e:
            logger.debug(f"Error getting URL: {e}")
            return ""
    
    @staticmethod
    async def wait_for_dom_stable(
        page: Page,
        timeout: int = 5000
    ) -> bool:
        """
        Wait for DOM to be stable (no more mutations).
        """
        try:
            # Wait for network idle
            await page.wait_for_load_state('networkidle', timeout=timeout)
            
            # Wait for a small additional time
            await page.wait_for_timeout(1000)
            
            return True
            
        except Exception as e:
            logger.debug(f"DOM stability wait failed: {e}")
            return False