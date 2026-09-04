"""
Configuration loader with environment variable support.
"""
import os
import logging
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Singleton configuration manager."""

    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            p1 = Path(__file__).parent.parent / "config" / "config.yaml"
            p2 = Path(__file__).parent.parent / "config.yaml"
            self._config_path = p1 if p1.exists() else p2
            self._config_mtime: Optional[float] = None
            self._load_config()

    def _load_config(self):
        """Load configuration from YAML file and override with env vars."""
        with open(self._config_path, 'r') as f:
            self._config = yaml.safe_load(f) or {}

        # Environment variable substitution
        self._substitute_env_vars(self._config)

        try:
            self._config_mtime = self._config_path.stat().st_mtime
        except OSError:
            self._config_mtime = None

    def reload_if_changed(self) -> bool:
        """
        Re-read config.yaml if it has changed on disk since last load.

        This is what makes the config.yaml kill switch (app.enabled: false)
        actually work at runtime - without it, the config would only ever
        be read once at process start and toggling the file while running
        would have no effect. Returns True if a reload happened.
        """
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return False

        if mtime != self._config_mtime:
            self._load_config()
            logger.info("config.yaml changed on disk - reloaded")
            return True
        return False

    def _substitute_env_vars(self, config_dict: Dict):
        """Recursively substitute ${ENV_VAR} placeholders."""
        for key, value in config_dict.items():
            if isinstance(value, dict):
                self._substitute_env_vars(value)
            elif isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                env_var = value[2:-1]
                config_dict[key] = os.getenv(env_var, '')
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value using dot notation."""
        keys = key.split('.')
        value = self._config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        
        return value
    
    @property
    def enabled(self) -> bool:
        # This is the config.yaml kill switch - always check for a fresh
        # value on disk rather than the one loaded at process start.
        self.reload_if_changed()
        return self.get('app.enabled', True)
    
    @property
    def browser_headless(self) -> bool:
        return self.get('browser.headless', False)
    
    @property
    def cdp_url(self) -> str:
        return self.get('browser.cdp_url', 'http://localhost:9222')
    
    @property
    def portal_url(self) -> str:
        return self.get('portal.url', 'https://www.usvisascheduling.com/')
    
    @property
    def telegram_enabled(self) -> bool:
        return self.get('notifications.telegram.enabled', False)
    
    @property
    def telegram_bot_token(self) -> str:
        return self.get('notifications.telegram.bot_token', '')
    
    @property
    def telegram_chat_id(self) -> str:
        return self.get('notifications.telegram.chat_id', '')

# Global config instance
config = Config()