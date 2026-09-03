"""
Logging configuration.
"""
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


def setup_logging():
    """Setup logging configuration."""
    from app.config import config

    # Create logs directory
    log_file = Path(config.get('logging.file', './logs/app.log'))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(config.get('logging.level', 'INFO'))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler with rotation
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=config.get('logging.max_size', 10485760),
        backupCount=config.get('logging.backup_count', 5)
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        config.get('logging.format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    logging.info(f"Logging initialized. Log file: {log_file}")