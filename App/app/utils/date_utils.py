"""
Date parsing and validation utilities.
"""
from typing import Optional, Tuple
from datetime import datetime, date, timedelta
import re
import logging

logger = logging.getLogger(__name__)


class DateUtils:
    """
    Utility functions for date parsing and manipulation.
    """
    
    # Common date formats
    DATE_FORMATS = [
        '%m/%d/%Y',      # 08/21/2026
        '%d/%m/%Y',      # 21/08/2026
        '%Y-%m-%d',      # 2026-08-21
        '%b %d, %Y',     # Aug 21, 2026
        '%B %d, %Y',     # August 21, 2026
        '%d %b %Y',      # 21 Aug 2026
        '%d %B %Y',      # 21 August 2026
    ]
    
    @staticmethod
    def parse_date(date_str: str) -> Optional[datetime]:
        """
        Parse date string to datetime object.
        """
        if not date_str:
            return None
        
        date_str = date_str.strip()
        
        for fmt in DateUtils.DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        # Try with regex fallback
        try:
            # Try to extract date components
            patterns = [
                r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})',  # MM/DD/YYYY
                r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})',  # YYYY-MM-DD
            ]
            
            for pattern in patterns:
                match = re.search(pattern, date_str)
                if match:
                    groups = match.groups()
                    if len(groups) == 3:
                        # Try to determine format
                        if len(groups[0]) == 4:  # YYYY-MM-DD
                            return datetime(int(groups[0]), int(groups[1]), int(groups[2]))
                        else:
                            # Try both MM/DD/YYYY and DD/MM/YYYY
                            try:
                                return datetime(int(groups[2]), int(groups[0]), int(groups[1]))
                            except ValueError:
                                return datetime(int(groups[2]), int(groups[1]), int(groups[0]))
        except Exception as e:
            logger.debug(f"Date parsing fallback failed: {e}")
        
        return None
    
    @staticmethod
    def is_valid_date(date_str: str) -> bool:
        """
        Check if string is a valid date.
        """
        return DateUtils.parse_date(date_str) is not None
    
    @staticmethod
    def format_date_for_fingerprint(date_str: str) -> str:
        """
        Format date consistently for fingerprinting.
        """
        dt = DateUtils.parse_date(date_str)
        if dt:
            return dt.strftime('%Y-%m-%d')
        return date_str
    
    @staticmethod
    def get_date_range(
        start_date: str,
        end_date: str
    ) -> list[str]:
        """
        Get all dates between start and end date.
        """
        start = DateUtils.parse_date(start_date)
        end = DateUtils.parse_date(end_date)
        
        if not start or not end:
            return []
        
        if start > end:
            start, end = end, start
        
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime('%Y-%m-%d'))
            current = current + timedelta(days=1)

        return dates
    
    @staticmethod
    def is_date_in_range(
        date_str: str,
        range_start: str,
        range_end: str
    ) -> bool:
        """
        Check if a date falls within a range.
        """
        dt = DateUtils.parse_date(date_str)
        start = DateUtils.parse_date(range_start)
        end = DateUtils.parse_date(range_end)
        
        if not dt or not start or not end:
            return False
        
        return start <= dt <= end
    
    @staticmethod
    def get_weekday(date_str: str) -> Optional[str]:
        """
        Get weekday name for a date.
        """
        dt = DateUtils.parse_date(date_str)
        if dt:
            return dt.strftime('%A')
        return None
    
    @staticmethod
    def is_weekend(date_str: str) -> bool:
        """
        Check if date is on weekend.
        """
        dt = DateUtils.parse_date(date_str)
        if dt:
            return dt.weekday() >= 5  # Saturday=5, Sunday=6
        return False
    
    @staticmethod
    def days_between(start_date: str, end_date: str) -> Optional[int]:
        """
        Get number of days between two dates.
        """
        start = DateUtils.parse_date(start_date)
        end = DateUtils.parse_date(end_date)
        
        if not start or not end:
            return None
        
        return (end - start).days