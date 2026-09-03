"""
Offline unit tests for the OFC dropdown/AJAX availability-checking flow,
against local mock-HTML fixtures that simulate the real portal's SPA
behavior: picking a location fires an AJAX call (simulated here with
setTimeout) that renders either "No Slots Available" or a calendar, with
no URL/navigation change.

Selectors are explicitly overridden to match these fixtures rather than
whatever's in the live config/config.yaml (which describes the real
portal and keeps changing as it's tuned) - without this, a mismatch would
send Playwright hunting for a selector that doesn't exist, falling
through to its ~30s default actionability timeout per attempt instead of
failing fast, and every test in this file would grind for minutes.
"""
from unittest import mock

import pytest

from app.monitor.availability import AvailabilityChecker
from tests.conftest import load_fixture

_TEST_SELECTORS = {
    'ofc_dropdown': '#ofc',
    'results_container': '#ofcResults',
    'calendar_container': '.calendar',
    'no_slots_pattern': r'no\s+slots?\s+available',
}


def _make_checker(tmp_path, locations):
    return AvailabilityChecker(
        fingerprint_storage_file=str(tmp_path / "fingerprints.json"),
        locations=locations,
        settle_timeout_ms=3000,
        jitter_range_seconds=(0.01, 0.02),  # keep tests fast
        selectors_override=_TEST_SELECTORS,
        interaction_delay_range_seconds=(0.001, 0.002),
        click_delay_range_ms=(1, 2),
    )


@pytest.mark.asyncio
async def test_select_location_detects_no_slots(page, tmp_path):
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["New Delhi"])

    calendar_appeared, glitched = await checker._select_location_and_wait(page, "New Delhi")

    assert calendar_appeared is False
    assert glitched is False


@pytest.mark.asyncio
async def test_select_location_detects_calendar(page, tmp_path):
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["Mumbai"])

    calendar_appeared, glitched = await checker._select_location_and_wait(page, "Mumbai")

    assert calendar_appeared is True
    assert glitched is False


@pytest.mark.asyncio
async def test_stale_result_is_cleared_between_locations(page, tmp_path):
    """
    Regression test for the staleness race: selecting a "no slots"
    location right after a location whose calendar was showing must not
    report a false calendar-appeared just because the old calendar
    hadn't been removed yet.
    """
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["Mumbai", "Chennai"])

    first, _ = await checker._select_location_and_wait(page, "Mumbai")
    second, _ = await checker._select_location_and_wait(page, "Chennai")

    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_check_availability_cycles_all_locations_and_finds_mumbai(page, tmp_path):
    """
    Full end-to-end cycle: only Mumbai has a calendar in the fixture: the
    other configured locations must be visited too (no early exit) and
    correctly report "no slots", while Mumbai's slot comes back validated
    and tagged with the location we explicitly selected.
    """
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["New Delhi", "Mumbai", "Chennai"])

    result = await checker.check_availability(page)

    assert result['has_slots'] is True
    assert len(result['valid_slots']) == 1

    slot = result['valid_slots'][0]['slot']
    assert slot['location'] == 'Mumbai'
    assert slot['date'] == '2026-08-21'
    assert slot['time'] == '09:30'


@pytest.mark.asyncio
async def test_check_availability_reports_location_statuses_in_configured_order(page, tmp_path):
    """
    location_statuses (fed into the Telegram cycle summary) must classify
    each location as 'No slots'/'Glitched'/'Slots Found' and be ordered
    by the configured location list, not the shuffled per-cycle visiting
    order - otherwise the summary would reorder itself unpredictably
    every cycle.
    """
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["New Delhi", "Mumbai", "Chennai"])

    result = await checker.check_availability(page)

    assert result['location_statuses'] == {
        "New Delhi": "No slots",
        "Mumbai": "Slots Found",
        "Chennai": "No slots",
    }


@pytest.mark.asyncio
async def test_check_availability_reports_glitched_location_status(page, tmp_path):
    """A glitched location (e.g. the PSE0501 native-alert case) must be reported as 'Glitched', not folded into 'No slots'."""
    await load_fixture(page, "ofc_dropdown_dialog_glitch.html")
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._settle_timeout_ms = 500  # short: the fixture never resolves, it's stuck "Loading..."

    result = await checker.check_availability(page)

    assert result['location_statuses'] == {"Mumbai": "Glitched"}


@pytest.mark.asyncio
async def test_check_availability_dedupes_on_repeat_check(page, tmp_path):
    """A slot already alerted must not be surfaced again on a later check."""
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["Mumbai"])

    first_result = await checker.check_availability(page)
    assert first_result['has_slots'] is True
    fingerprint = first_result['valid_slots'][0]['fingerprint']['fingerprint']
    checker.mark_alerted(fingerprint)

    await load_fixture(page, "ofc_dropdown.html")
    second_result = await checker.check_availability(page)

    assert second_result['has_slots'] is False
    assert len(second_result['duplicate_slots']) == 1


@pytest.mark.asyncio
async def test_dialog_is_auto_accepted_and_marked_glitched(page, tmp_path):
    """
    Regression test for the real "PSE0501" native-alert glitch: a dialog
    firing during selection must be auto-accepted (not hang) and the
    attempt must be reported as glitched, not as a genuine "no slots".
    """
    await load_fixture(page, "ofc_dropdown_dialog_glitch.html")
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._settle_timeout_ms = 500  # short: the fixture never resolves, it's stuck "Loading..."

    calendar_appeared, glitched = await checker._select_location_and_wait(page, "Mumbai")

    assert calendar_appeared is False
    assert glitched is True
    assert not page.is_closed()  # the dialog didn't hang/crash the page


@pytest.mark.asyncio
async def test_full_cycle_glitch_triggers_conservative_recovery(page, tmp_path):
    """
    When every location in a cycle glitches, recovery must eventually
    fire - but only after the configured number of fully-glitched cycles
    and a (here, shrunk-for-the-test) human-like pause, never instantly.
    The page has no real navigation history (content was injected via
    set_content, not goto()), so this also exercises the go_back()
    failure -> reload() fallback path.
    """
    await load_fixture(page, "ofc_dropdown_dialog_glitch.html")
    checker = _make_checker(tmp_path, ["Mumbai", "New Delhi"])
    checker._settle_timeout_ms = 300
    checker._reload_backoff_min_seconds = 0.05
    checker._reload_backoff_max_seconds = 0.1
    checker._min_glitched_cycles_before_reload = 1

    result = await checker.check_availability(page)

    assert result['glitched_locations'] == 2
    assert result['recovered'] is True


@pytest.mark.asyncio
async def test_recovery_waits_for_configured_consecutive_glitched_cycles(page, tmp_path):
    """A single glitched cycle must not trigger recovery if the configured threshold is higher."""
    await load_fixture(page, "ofc_dropdown_dialog_glitch.html")
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._settle_timeout_ms = 300
    checker._reload_backoff_min_seconds = 0.05
    checker._reload_backoff_max_seconds = 0.1
    checker._min_glitched_cycles_before_reload = 2

    first = await checker.check_availability(page)
    assert first['recovered'] is False
    assert checker._consecutive_glitched_cycles == 1

    second = await checker.check_availability(page)
    assert second['recovered'] is True
    assert checker._consecutive_glitched_cycles == 0


@pytest.mark.asyncio
async def test_conservative_recovery_uses_history_when_available(page, tmp_path):
    """
    When real navigation history exists, recovery goes back then forward
    through it rather than reloading - data: URLs build real history
    (unlike page.set_content(), which doesn't) with no network involved.
    """
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._reload_backoff_min_seconds = 0.01
    checker._reload_backoff_max_seconds = 0.02

    await page.goto("data:text/html,<title>Page One</title><body>One</body>")
    await page.goto("data:text/html,<title>Page Two</title><body>Two</body>")

    await checker._conservative_recovery(page)

    title = await page.title()
    assert title == "Page Two"  # round-tripped back to where it started


@pytest.mark.asyncio
async def test_conservative_recovery_falls_back_to_reload_without_history(page, tmp_path):
    """
    With no real navigation history (only set_content() was used),
    recovery must fall back to reload() rather than raise.
    """
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._reload_backoff_min_seconds = 0.01
    checker._reload_backoff_max_seconds = 0.02

    await checker._conservative_recovery(page)  # must not raise

    assert not page.is_closed()


@pytest.mark.asyncio
async def test_check_availability_shuffles_location_order_each_cycle(page, tmp_path):
    """
    Regression test: the configured order (self._locations) must stay
    untouched (other code, e.g. logging, may reasonably expect it), while
    the actual per-cycle iteration order is a shuffled COPY - verified via
    a mock rather than a statistical/flaky check on real randomness.
    """
    await load_fixture(page, "ofc_dropdown.html")
    checker = _make_checker(tmp_path, ["New Delhi", "Mumbai", "Chennai"])
    original_locations = list(checker._locations)

    with mock.patch("app.monitor.availability.random.shuffle") as mock_shuffle:
        await checker.check_availability(page)

    assert mock_shuffle.called
    shuffled_arg = mock_shuffle.call_args[0][0]
    assert shuffled_arg == original_locations  # same elements (our mock didn't actually shuffle)
    assert shuffled_arg is not checker._locations  # a COPY was passed in, not the original list
    assert checker._locations == original_locations  # configured order left untouched


@pytest.mark.asyncio
async def test_notifies_once_after_persistent_glitch_streak(page, tmp_path):
    """
    A streak of fully-glitched cycles that recovery keeps failing to fix
    must trigger exactly one needs_attention notice at the configured
    threshold - not before, and not again on every subsequent cycle.
    """
    await load_fixture(page, "ofc_dropdown_dialog_glitch.html")
    checker = _make_checker(tmp_path, ["Mumbai"])
    checker._settle_timeout_ms = 300
    checker._reload_backoff_min_seconds = 0.01
    checker._reload_backoff_max_seconds = 0.02
    checker._min_glitched_cycles_before_reload = 1
    checker._notify_after_glitched_cycles = 2

    first = await checker.check_availability(page)
    assert first['needs_attention'] is False
    assert first['persistent_glitch_streak'] == 1

    second = await checker.check_availability(page)
    assert second['needs_attention'] is True
    assert second['attention_reason']
    assert second['persistent_glitch_streak'] == 2

    third = await checker.check_availability(page)
    assert third['needs_attention'] is False  # already notified this streak
    assert third['persistent_glitch_streak'] == 3
