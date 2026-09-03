"""
Error classification and handling with recovery strategies.
"""
from enum import Enum
from typing import Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import traceback

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categories of errors for handling strategy."""
    # Transient - can retry
    TRANSIENT = "transient"
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    
    # Permanent - need human intervention
    PERMANENT = "permanent"
    AUTHENTICATION = "authentication"
    SESSION_EXPIRED = "session_expired"
    CAPTCHA = "captcha"
    ACCESS_DENIED = "access_denied"
    MAINTENANCE = "maintenance"
    
    # Unknown
    UNKNOWN = "unknown"


@dataclass
class ErrorInfo:
    """Information about an error."""
    error_type: ErrorCategory
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    retry_count: int = 0
    original_exception: Optional[Exception] = None
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    
    def increment_retry(self):
        self.retry_count += 1
        self.timestamp = datetime.now()


class ErrorHandler:
    """
    Handles errors with classification and recovery strategies.
    """
    
    def __init__(self):
        self._max_retries = 3
        self._consecutive_errors = 0
        self._last_error_time: Optional[datetime] = None
        self._error_history: list[ErrorInfo] = []
        self._recovery_callbacks: Dict[ErrorCategory, list[Callable[[ErrorInfo], Awaitable[None]]]] = {
            category: [] for category in ErrorCategory
        }
        
        # Track which errors need immediate human attention
        self._human_required_errors = {
            ErrorCategory.CAPTCHA,
            ErrorCategory.ACCESS_DENIED,
            ErrorCategory.AUTHENTICATION,
            ErrorCategory.SESSION_EXPIRED,
            ErrorCategory.MAINTENANCE,
        }
    
    def register_recovery_callback(
        self, 
        category: ErrorCategory, 
        callback: Callable[[ErrorInfo], Awaitable[None]]
    ):
        """
        Register a callback for specific error category recovery.
        
        Args:
            category: Error category
            callback: Async function that takes ErrorInfo and handles recovery
        """
        self._recovery_callbacks[category].append(callback)
        logger.debug(f"Registered recovery callback for {category.value}")
    
    def classify_error(self, exception: Exception, context: Dict[str, Any] = None) -> ErrorCategory:
        """
        Classify an error into a category.
        
        Args:
            exception: The exception to classify
            context: Additional context for classification
            
        Returns:
            ErrorCategory
        """
        exception_str = str(exception).lower()
        
        # Network errors
        if any(keyword in exception_str for keyword in ['timeout', 'timed out', 'time out']):
            return ErrorCategory.TIMEOUT
        
        if any(keyword in exception_str for keyword in ['network', 'connection', 'socket', 'refused']):
            return ErrorCategory.NETWORK
        
        # Rate limiting
        if any(keyword in exception_str for keyword in ['rate limit', 'too many', '429', 'throttl']):
            return ErrorCategory.RATE_LIMIT
        
        # Authentication/Session
        if any(keyword in exception_str for keyword in ['auth', 'login', 'session', 'expired']):
            return ErrorCategory.SESSION_EXPIRED
        
        # CAPTCHA
        if any(keyword in exception_str for keyword in ['captcha', 'challenge', 'verify']):
            return ErrorCategory.CAPTCHA
        
        # Access denied
        if any(keyword in exception_str for keyword in ['denied', 'forbidden', '403', 'blocked']):
            return ErrorCategory.ACCESS_DENIED
        
        # If context is provided, check it
        if context:
            if context.get('state') in ['maintenance']:
                return ErrorCategory.MAINTENANCE
            if context.get('state') in ['captcha']:
                return ErrorCategory.CAPTCHA
        
        # Check if it's a known exception type
        exception_type = type(exception).__name__
        if exception_type in ['TimeoutError', 'TimeoutException']:
            return ErrorCategory.TIMEOUT
        
        if exception_type in ['ConnectionError', 'ConnectionRefusedError']:
            return ErrorCategory.NETWORK
        
        # Default to transient if it might be temporary
        if self._consecutive_errors < 3:
            return ErrorCategory.TRANSIENT
        
        # If too many consecutive errors, might be permanent
        return ErrorCategory.UNKNOWN
    
    async def handle_error(
        self, 
        exception: Exception, 
        context: Dict[str, Any] = None,
        action: str = "unknown"
    ) -> Dict[str, Any]:
        """
        Handle an error with appropriate strategy.
        
        Args:
            exception: The exception that occurred
            context: Additional context
            action: What action was being performed
            
        Returns:
            Dict with handling result
        """
        # Classify error
        category = self.classify_error(exception, context)
        
        # Create error info
        error_info = ErrorInfo(
            error_type=category,
            message=str(exception),
            original_exception=exception,
            stack_trace=traceback.format_exc(),
            context=context or {}
        )
        
        # Update tracking
        self._consecutive_errors += 1
        self._last_error_time = datetime.now()
        self._error_history.append(error_info)
        
        # Keep history manageable
        if len(self._error_history) > 100:
            self._error_history = self._error_history[-100:]
        
        # Log error
        log_level = logging.ERROR if category in self._human_required_errors else logging.WARNING
        logger.log(
            log_level,
            f"Error during {action}: {category.value} - {exception}"
        )
        
        # Determine if human intervention required
        requires_human = category in self._human_required_errors
        
        # Determine if retry is possible
        can_retry = (
            category in [ErrorCategory.TRANSIENT, ErrorCategory.NETWORK, ErrorCategory.TIMEOUT] 
            and error_info.retry_count < self._max_retries
        )
        
        # Execute recovery callbacks
        if category in self._recovery_callbacks:
            for callback in self._recovery_callbacks[category]:
                try:
                    await callback(error_info)
                except Exception as e:
                    logger.error(f"Recovery callback failed: {e}")
        
        return {
            'category': category,
            'requires_human': requires_human,
            'can_retry': can_retry,
            'retry_count': error_info.retry_count,
            'max_retries': self._max_retries,
            'error_info': error_info,
            'consecutive_errors': self._consecutive_errors,
        }
    
    def reset_consecutive_errors(self):
        """Reset consecutive error counter (call after success)."""
        self._consecutive_errors = 0
        logger.debug("Consecutive errors reset")
    
    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics."""
        return {
            'consecutive_errors': self._consecutive_errors,
            'total_errors': len(self._error_history),
            'last_error_time': self._last_error_time,
            'error_distribution': self._get_error_distribution(),
        }
    
    def _get_error_distribution(self) -> Dict[str, int]:
        """Get distribution of error categories."""
        distribution = {}
        for error in self._error_history[-100:]:  # Last 100 errors
            key = error.error_type.value
            distribution[key] = distribution.get(key, 0) + 1
        return distribution
    
    def should_continue(self) -> bool:
        """
        Determine if monitoring should continue based on error rate.
        
        Returns:
            bool: True if should continue, False if need to stop
        """
        # If >5 consecutive errors, stop
        if self._consecutive_errors > 5:
            logger.error(f"Too many consecutive errors: {self._consecutive_errors}")
            return False
        
        # If >50% errors in last 10 attempts, stop
        if len(self._error_history) >= 10:
            recent_errors = [e for e in self._error_history[-10:] 
                           if e.error_type in [ErrorCategory.PERMANENT, ErrorCategory.ACCESS_DENIED]]
            if len(recent_errors) >= 5:
                logger.error(f"High error rate: {len(recent_errors)}/10 errors")
                return False
        
        return True