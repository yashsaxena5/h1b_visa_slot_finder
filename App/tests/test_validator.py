"""
Offline unit tests for SlotValidator's 8-step validation, against a local
mock-HTML fixture with one genuinely selectable slot and one
"looks available but is actually disabled" false positive.

filters_override is used explicitly so these tests validate against a
fixed, known filter set rather than the live config/config.yaml (which
describes the real portal, e.g. "MUMBAI VAC" rather than "Mumbai", and
will keep changing as it's tuned) - without it, these would silently
start failing every time the real location names change.
"""
import pytest

from app.monitor.validator import SlotValidator
from tests.conftest import load_fixture

_TEST_FILTERS = {
    'category': 'H-1B',
    'locations': ['Mumbai'],
    'appointment_types': ['Interview'],
}


@pytest.mark.asyncio
async def test_valid_slot_passes_all_steps(page):
    await load_fixture(page, "slots_available.html")
    validator = SlotValidator(filters_override=_TEST_FILTERS)

    slot = {
        'date': '2026-08-21',
        'time': '09:30',
        'location': 'Mumbai',
        'category': 'H-1B',
        'appointment_type': 'Interview',
    }
    result = await validator.validate_slot(page, slot)

    assert result.is_valid is True
    assert result.step_results['category'] is True
    assert result.step_results['location'] is True
    assert result.step_results['appointment_type'] is True
    assert result.step_results['date_selectable'] is True
    assert result.step_results['time_selectable'] is True


@pytest.mark.asyncio
async def test_disabled_slot_fails_selectability(page):
    """
    A slot that LOOKS available in the DOM (matches the same extraction
    selectors as a real one) but is aria-disabled must be rejected - this
    is exactly the false-positive protection the 8-step check exists for.
    """
    await load_fixture(page, "slots_available.html")
    validator = SlotValidator(filters_override=_TEST_FILTERS)

    slot = {
        'date': '2026-08-22',
        'time': '10:00',
        'location': 'Mumbai',
        'category': 'H-1B',
        'appointment_type': 'Interview',
    }
    result = await validator.validate_slot(page, slot)

    assert result.is_valid is False
    assert result.step_results['date_selectable'] is False


@pytest.mark.asyncio
async def test_wrong_location_rejected(page):
    await load_fixture(page, "slots_available.html")
    validator = SlotValidator(filters_override=_TEST_FILTERS)

    slot = {
        'date': '2026-08-21',
        'time': '09:30',
        'location': 'Vancouver',  # not in the configured allow-list
        'category': 'H-1B',
        'appointment_type': 'Interview',
    }
    result = await validator.validate_slot(page, slot)

    assert result.is_valid is False
    assert result.step_results['location'] is False


@pytest.mark.asyncio
async def test_wrong_category_rejected(page):
    await load_fixture(page, "slots_available.html")
    validator = SlotValidator(filters_override=_TEST_FILTERS)

    slot = {
        'date': '2026-08-21',
        'time': '09:30',
        'location': 'Mumbai',
        'category': 'B1/B2',
        'appointment_type': 'Interview',
    }
    result = await validator.validate_slot(page, slot)

    assert result.is_valid is False
    assert result.step_results['category'] is False
