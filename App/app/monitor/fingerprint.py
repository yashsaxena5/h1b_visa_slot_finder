"""
SHA-256 fingerprinting for slot deduplication.
"""
import hashlib
import json
from typing import Optional, Dict, Any, List, Set
from pathlib import Path
from datetime import datetime
import logging

from app.utils.date_utils import DateUtils

logger = logging.getLogger(__name__)


class SlotFingerprint:
    """
    Manages SHA-256 fingerprints for slot deduplication.
    """
    
    def __init__(self, storage_file: str = "./data/fingerprints.json"):
        """
        Initialize fingerprint manager.
        
        Args:
            storage_file: Path to JSON storage file
        """
        self.storage_file = Path(storage_file)
        self.fingerprints: Dict[str, Dict[str, Any]] = {}
        self.alerted_fingerprints: Set[str] = set()
        self._loaded = False
        
        # Ensure directory exists
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing fingerprints
        self.load()
    
    def load(self):
        """Load fingerprints from storage."""
        if self._loaded:
            return
        
        try:
            if self.storage_file.exists():
                with open(self.storage_file, 'r') as f:
                    data = json.load(f)
                    self.fingerprints = data.get('fingerprints', {})
                    self.alerted_fingerprints = set(data.get('alerted', []))
                    
                logger.info(f"Loaded {len(self.fingerprints)} fingerprints")
            else:
                logger.info("No existing fingerprint file found, starting fresh")
            
            self._loaded = True
            
        except Exception as e:
            logger.error(f"Failed to load fingerprints: {e}")
            self.fingerprints = {}
            self.alerted_fingerprints = set()
            self._loaded = True
    
    def save(self):
        """Save fingerprints to storage."""
        try:
            data = {
                'fingerprints': self.fingerprints,
                'alerted': list(self.alerted_fingerprints),
                'last_updated': datetime.now().isoformat(),
                'total_fingerprints': len(self.fingerprints),
                'total_alerted': len(self.alerted_fingerprints),
            }
            
            with open(self.storage_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug(f"Saved {len(self.fingerprints)} fingerprints")
            
        except Exception as e:
            logger.error(f"Failed to save fingerprints: {e}")
    
    def generate_fingerprint(
        self,
        category: str,
        location: str,
        appointment_type: str,
        date: str,
        time: str = ""
    ) -> str:
        """
        Generate SHA-256 fingerprint for a slot.
        
        Args:
            category: Visa category (e.g., H-1B)
            location: Consular location (e.g., Mumbai)
            appointment_type: Type (Interview/Drop-off)
            date: Appointment date
            time: Appointment time (optional)
            
        Returns:
            SHA-256 fingerprint string
        """
        # Normalize date format
        normalized_date = DateUtils.format_date_for_fingerprint(date)
        
        # Create fingerprint string
        fingerprint_parts = [
            category.strip().upper(),
            location.strip().upper(),
            appointment_type.strip().upper(),
            normalized_date,
            time.strip().upper() if time else "",
        ]
        
        fingerprint_str = "|".join(fingerprint_parts)
        
        # Generate SHA-256
        fingerprint = hashlib.sha256(
            fingerprint_str.encode('utf-8')
        ).hexdigest()
        
        return fingerprint
    
    def check_and_record(
        self,
        category: str,
        location: str,
        appointment_type: str,
        date: str,
        time: str = ""
    ) -> Dict[str, Any]:
        """
        Check if slot is new and record it.

        Returns:
            Dict with:
                - is_new: bool
                - already_alerted: bool
                - should_alert: bool - True unless this exact slot has
                  already been successfully alerted on. This is the field
                  callers should gate on, NOT is_new: a slot that was seen
                  before but never successfully alerted (e.g. the alert
                  layer failed or crashed) must still be surfaced, not
                  silently dropped forever just because it isn't "new".
                - fingerprint: str
                - first_seen: str (ISO date)
                - last_seen: str (ISO date)
                - occurrences: int
        """
        fingerprint = self.generate_fingerprint(
            category, location, appointment_type, date, time
        )

        now = datetime.now().isoformat()

        # Check if fingerprint exists
        if fingerprint in self.fingerprints:
            record = self.fingerprints[fingerprint]
            record['last_seen'] = now
            record['occurrences'] += 1
            self.save()

            already_alerted = fingerprint in self.alerted_fingerprints

            return {
                'is_new': False,
                'already_alerted': already_alerted,
                'should_alert': not already_alerted,
                'fingerprint': fingerprint,
                'first_seen': record['first_seen'],
                'last_seen': record['last_seen'],
                'occurrences': record['occurrences'],
            }
        else:
            # New fingerprint
            record = {
                'fingerprint': fingerprint,
                'category': category,
                'location': location,
                'appointment_type': appointment_type,
                'date': date,
                'time': time,
                'first_seen': now,
                'last_seen': now,
                'occurrences': 1,
            }

            self.fingerprints[fingerprint] = record
            self.save()

            return {
                'is_new': True,
                'already_alerted': False,
                'should_alert': True,
                'fingerprint': fingerprint,
                'first_seen': record['first_seen'],
                'last_seen': record['last_seen'],
                'occurrences': 1,
            }
    
    def mark_as_alerted(self, fingerprint: str) -> bool:
        """
        Mark a fingerprint as alerted.
        
        Returns:
            bool: True if newly marked, False if already alerted
        """
        if fingerprint in self.alerted_fingerprints:
            return False
        
        if fingerprint in self.fingerprints:
            self.alerted_fingerprints.add(fingerprint)
            self.fingerprints[fingerprint]['alerted_at'] = datetime.now().isoformat()
            self.save()
            return True
        
        return False
    
    def is_alerted(self, fingerprint: str) -> bool:
        """
        Check if a fingerprint has been alerted.
        """
        return fingerprint in self.alerted_fingerprints
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about fingerprints.
        """
        total = len(self.fingerprints)
        alerted = len(self.alerted_fingerprints)
        
        # Get recent fingerprints (last 24 hours)
        now = datetime.now()
        recent = 0
        for record in self.fingerprints.values():
            try:
                first_seen = datetime.fromisoformat(record['first_seen'])
                if (now - first_seen).total_seconds() < 86400:  # 24 hours
                    recent += 1
            except:
                pass
        
        # Get distribution by location
        locations = {}
        for record in self.fingerprints.values():
            loc = record.get('location', 'Unknown')
            locations[loc] = locations.get(loc, 0) + 1
        
        return {
            'total_fingerprints': total,
            'total_alerted': alerted,
            'alerted_percentage': (alerted / total * 100) if total > 0 else 0,
            'recent_fingerprints': recent,
            'locations': locations,
        }
    
    def cleanup(self, older_than_days: int = 90):
        """
        Clean up old fingerprints.
        """
        if older_than_days <= 0:
            return
        
        now = datetime.now()
        to_delete = []
        
        for fp, record in self.fingerprints.items():
            try:
                last_seen = datetime.fromisoformat(record['last_seen'])
                if (now - last_seen).days > older_than_days:
                    to_delete.append(fp)
            except:
                continue
        
        for fp in to_delete:
            del self.fingerprints[fp]
            if fp in self.alerted_fingerprints:
                self.alerted_fingerprints.remove(fp)
        
        if to_delete:
            logger.info(f"Cleaned up {len(to_delete)} old fingerprints")
            self.save()
    
    def reset(self):
        """
        Reset all fingerprints (use with caution).
        """
        self.fingerprints = {}
        self.alerted_fingerprints = set()
        self.save()
        logger.warning("All fingerprints have been reset")