"""
Offline unit tests for StateMachine's routing/transition logic. No
browser or network involved - these exercise the pure state-transition
logic and directly regression-test the safety-critical bugs that were
fixed (CAPTCHA/session-expiry no longer bypassing the halt, the
error-handler dict/attribute mismatch, and the unhandled-state busy loop).

A stub AlertManager is injected so these tests never fire a real desktop
notification, sound alarm, or Telegram call.
"""
import pytest

from app.detection import ErrorCategory
from app.monitor.availability import AvailabilityChecker
from app.monitor.state_machine import StateMachine, MachineState
from tests.conftest import StubAlertManager


@pytest.fixture
def sm(tmp_path):
    # Isolate the fingerprint store so tests never touch (or get polluted
    # by) the real ./data/fingerprints.json used by the running app.
    checker = AvailabilityChecker(fingerprint_storage_file=str(tmp_path / "fingerprints.json"))
    return StateMachine(availability_checker=checker, alert_manager=StubAlertManager())


class _FakeSession:
    """Stands in for SessionManager for _run_navigation() tests - no real browser needed."""

    def __init__(self, url: str, state: str = 'unknown', confidence: float = 0.3):
        self._url = url
        self._state = state
        self._confidence = confidence

    async def navigate_to_portal(self):
        return True

    async def detect_page_state(self, force_refresh: bool = True):
        return {
            'state': self._state,
            'confidence': self._confidence,
            'details': 'x',
            'detection_method': 'x',
            'url': self._url,
        }


class _FakeBrowserLauncher:
    def __init__(self, session):
        self.session = session


@pytest.mark.asyncio
async def test_captcha_halts_to_human_required(sm):
    """
    Regression test: a detected CAPTCHA must never be treated as a valid,
    navigable session - it must halt to HUMAN_REQUIRED (see the
    session_expiry.py _determine_validity fix).
    """
    await sm._route_by_state({'state': 'captcha', 'confidence': 0.95, 'details': 'x'})
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_session_expired_halts_to_human_required(sm):
    await sm._route_by_state({'state': 'session_expired', 'confidence': 0.9, 'details': 'x'})
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_access_denied_halts_to_human_required(sm):
    await sm._route_by_state({'state': 'access_denied', 'confidence': 0.9, 'details': 'x'})
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_maintenance_backs_off_not_human_required(sm):
    """Maintenance is expected/scheduled - it should back off, not wake a human."""
    await sm._route_by_state({'state': 'maintenance', 'confidence': 0.95, 'details': 'x'})
    assert sm.get_current_state() == MachineState.BACKOFF_WAIT


@pytest.mark.asyncio
async def test_normal_state_does_not_route(sm):
    routed = await sm._route_by_state({'state': 'dashboard', 'confidence': 0.6, 'details': 'x'})
    assert routed is False
    assert sm.get_current_state() == MachineState.INIT  # unchanged


@pytest.mark.asyncio
async def test_unrecognized_state_on_portal_waits_instead_of_halting(sm):
    """
    Regression test: landing on the homepage (or any recognized-domain
    page that isn't yet dashboard/appointment/scheduling) must wait and
    retry, not immediately halt to HUMAN_REQUIRED - that would make the
    bot unusable every time it starts before you've navigated to the
    exact scheduling page yourself.
    """
    sm._browser_launcher = _FakeBrowserLauncher(
        _FakeSession(url="https://www.usvisascheduling.com/en-US/")
    )

    await sm._run_navigation()

    assert sm.get_current_state() == MachineState.BACKOFF_WAIT
    assert sm._unrecognized_state_streak == 1


@pytest.mark.asyncio
async def test_unrecognized_state_off_portal_halts_immediately(sm):
    """If we're not even on the portal domain, that's a real reason to stop."""
    sm._browser_launcher = _FakeBrowserLauncher(
        _FakeSession(url="https://example.com/somewhere-else")
    )

    await sm._run_navigation()

    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_unrecognized_state_escalates_after_max_retries(sm):
    """A genuinely stuck state must still eventually surface to a human."""
    session = _FakeSession(url="https://www.usvisascheduling.com/en-US/")
    sm._browser_launcher = _FakeBrowserLauncher(session)
    sm._max_unrecognized_state_retries = 2

    await sm._run_navigation()
    assert sm.get_current_state() == MachineState.BACKOFF_WAIT
    await sm._run_navigation()
    assert sm.get_current_state() == MachineState.BACKOFF_WAIT
    await sm._run_navigation()
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_recognized_state_resets_unrecognized_streak(sm):
    session = _FakeSession(
        url="https://www.usvisascheduling.com/en-US/ofc-schedule/",
        state='scheduling', confidence=0.8,
    )
    sm._browser_launcher = _FakeBrowserLauncher(session)
    sm._unrecognized_state_streak = 5

    await sm._run_navigation()

    assert sm.get_current_state() == MachineState.CHECK_AVAILABILITY
    assert sm._unrecognized_state_streak == 0


@pytest.mark.asyncio
async def test_human_required_sends_notice_via_alert_manager(sm):
    await sm._route_by_state({'state': 'captcha', 'confidence': 0.95, 'details': 'x'})
    assert len(sm._alert_manager.human_required_notices) == 1


@pytest.mark.asyncio
async def test_error_state_requires_human_routes_correctly(sm):
    """
    Regression test for the error_info.category AttributeError bug -
    _handle_error_state must read the error_result dict correctly and
    honor its requires_human/can_retry classification instead of
    re-deriving (and mis-deriving) its own.
    """
    error_result = {
        'category': ErrorCategory.CAPTCHA,
        'requires_human': True,
        'can_retry': False,
    }
    await sm.transition(MachineState.ERROR, {'error_result': error_result, 'action': 'test'})
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_error_state_retryable_goes_to_backoff(sm):
    error_result = {
        'category': ErrorCategory.NETWORK,
        'requires_human': False,
        'can_retry': True,
    }
    await sm.transition(MachineState.ERROR, {'error_result': error_result, 'action': 'test'})
    assert sm.get_current_state() == MachineState.BACKOFF


@pytest.mark.asyncio
async def test_error_state_without_classification_fails_safe(sm):
    """An exception that reaches ERROR without a structured error_result
    must still halt for a human, not stall silently."""
    await sm.transition(MachineState.ERROR, {'error': 'boom'})
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_unhandled_state_fails_safe_to_human_required(sm):
    """
    Regression test for the busy-loop-on-unhandled-state bug: any state
    _run_state doesn't know how to dispatch must halt, never spin
    silently on a 1s sleep forever.
    """
    sm._current_state = MachineState.BACKOFF_WAIT  # simulate a hypothetical gap
    await sm._run_state()
    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_human_required_stays_parked_without_self_transition(sm):
    """
    Regression test for a real bug hit in production: run()'s loop calls
    _run_state() unconditionally at the top of every iteration, including
    on the second and later iterations while parked in HUMAN_REQUIRED
    (the loop's own "if HUMAN_REQUIRED: sleep; continue" check only runs
    *after* _run_state() dispatches). Without an explicit HUMAN_REQUIRED
    case, this fell into the unhandled-state catch-all and produced an
    infinite human_required -> human_required self-transition every loop.
    """
    await sm.transition(MachineState.HUMAN_REQUIRED, {'reason': 'test'})
    transitions_before = len(sm.get_transition_history())

    await sm._run_state()

    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED
    assert len(sm.get_transition_history()) == transitions_before  # no new (self-)transition


@pytest.mark.asyncio
async def test_alert_flow_marks_fingerprint_alerted(sm):
    """
    A slot alert must only be marked "alerted" (suppressing future
    dedup) after send_slot_alert() actually ran - see the
    should_alert-vs-is_new fingerprint fix.
    """
    fp_result = sm._availability_checker._fingerprint.check_and_record(
        category='H-1B', location='Mumbai', appointment_type='Interview',
        date='2026-08-21', time='09:30',
    )
    slot = {
        'date': '2026-08-21', 'time': '09:30', 'location': 'Mumbai',
        'category': 'H-1B', 'appointment_type': 'Interview',
    }
    sm._state_data[MachineState.ALERT.value] = {
        'valid_slots': [{'slot': slot, 'fingerprint': fp_result}]
    }

    await sm._run_alert()

    assert sm.get_current_state() == MachineState.PAUSE
    assert len(sm._alert_manager.slot_alerts) == 1
    assert sm._availability_checker._fingerprint.is_alerted(fp_result['fingerprint'])


class _FakeSessionWithPage:
    """Reports a normal/unrecognized-but-safe state, so the pre-check in
    _run_check_availability() passes through without needing a real page."""

    async def get_page(self):
        return None  # unused - the fake checker below ignores it

    async def detect_page_state(self, force_refresh: bool = True):
        return {
            'state': 'unknown', 'confidence': 0.3, 'details': 'x', 'detection_method': 'x',
            'url': 'https://www.usvisascheduling.com/en-US/ofc-schedule/',
        }


class _StatefulFakeSession:
    """Returns a different detect_page_state() result on each successive
    call, in order - simulates the page changing between the pre-check
    and post-check within a single _run_check_availability() call."""

    def __init__(self, states):
        self._states = list(states)  # list of (state, confidence) tuples
        self._call_count = 0

    async def get_page(self):
        return None

    async def navigate_to_portal(self):
        return True

    async def detect_page_state(self, force_refresh: bool = True):
        idx = min(self._call_count, len(self._states) - 1)
        state, confidence = self._states[idx]
        self._call_count += 1
        return {
            'state': state, 'confidence': confidence, 'details': 'x', 'detection_method': 'x',
            'url': 'https://www.usvisascheduling.com/en-US/ofc-schedule/',
        }


class _FakeAvailabilityChecker:
    def __init__(self, result):
        self._result = result

    async def check_availability(self, page):
        return self._result


@pytest.mark.asyncio
async def test_persistent_glitch_streak_sends_notice_without_halting(sm):
    """
    A persistent-glitch notice (e.g. repeated "PSE0501" errors recovery
    keeps failing to clear) must reach the human via the alert manager,
    but must NOT halt automation the way CAPTCHA/session-expiry do -
    there's nothing for a human to unblock here, recovery keeps retrying
    on its own; a silent multi-cycle backend outage just shouldn't be
    invisible.
    """
    sm._browser_launcher = _FakeBrowserLauncher(_FakeSessionWithPage())
    sm._availability_checker = _FakeAvailabilityChecker({
        'has_slots': False,
        'needs_attention': True,
        'attention_reason': 'Backend has been failing for 3 consecutive cycles',
    })

    await sm._run_check_availability()

    assert len(sm._alert_manager.human_required_notices) == 1
    assert 'Backend has been failing' in sm._alert_manager.human_required_notices[0]
    assert sm.get_current_state() != MachineState.HUMAN_REQUIRED
    assert sm.get_current_state() == MachineState.BACKOFF_WAIT


@pytest.mark.asyncio
async def test_check_availability_pre_check_catches_already_blocked_page(sm):
    """
    If the page has already turned into an access-denied/CAPTCHA/etc.
    page before CHECK_AVAILABILITY even starts, it must halt immediately
    rather than handing off to AvailabilityChecker to grind against a
    dead dropdown.
    """
    sm._browser_launcher = _FakeBrowserLauncher(_StatefulFakeSession([('access_denied', 0.9)]))
    sm._availability_checker = _FakeAvailabilityChecker({'has_slots': False})  # must never be reached

    await sm._run_check_availability()

    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_check_availability_post_check_catches_page_blocked_mid_cycle(sm):
    """
    Regression test for a real production incident: the portal blocked
    calendar access ("Access limitation ... Prohibited conduct") partway
    through a run. Every location started failing with "dropdown not
    found", and the bot kept treating that as a routine glitch instead of
    recognizing the block, grinding through recovery for a full extra
    cycle before a human noticed and killed it. If a cycle comes back
    fully glitched, state must be re-checked immediately rather than
    trusting the glitch-recovery machinery (built for transient hiccups)
    to keep going on its own.
    """
    session = _StatefulFakeSession([
        ('unknown', 0.3),          # pre-check: looked fine, proceed
        ('access_denied', 0.9),    # post-check: page changed mid-cycle
    ])
    sm._browser_launcher = _FakeBrowserLauncher(session)
    sm._availability_checker = _FakeAvailabilityChecker({
        'has_slots': False,
        'glitched_locations': 5,
        'locations_checked': 5,
    })

    await sm._run_check_availability()

    assert sm.get_current_state() == MachineState.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_check_availability_sends_cycle_summary_every_time(sm):
    """
    Routine telemetry must go out via the alert manager after every
    check_availability() call, regardless of outcome - AlertManager
    itself is responsible for throttling how often that turns into an
    actual Telegram send (see tests/test_notifications.py).
    """
    sm._browser_launcher = _FakeBrowserLauncher(_FakeSessionWithPage())
    sm._availability_checker = _FakeAvailabilityChecker({
        'has_slots': False,
        'location_statuses': {'MUMBAI VAC': 'No slots', 'CHENNAI VAC': 'Glitched'},
    })

    await sm._run_check_availability()

    assert sm._alert_manager.cycle_summaries == [
        {'MUMBAI VAC': 'No slots', 'CHENNAI VAC': 'Glitched'}
    ]


@pytest.mark.asyncio
async def test_check_availability_partial_glitch_does_not_over_trigger_recheck(sm):
    """A single flaky location (not the whole cycle) must not trigger the extra state re-check."""
    session = _StatefulFakeSession([
        ('unknown', 0.3),
        ('access_denied', 0.9),  # would wrongly halt if the post-check fired here
    ])
    sm._browser_launcher = _FakeBrowserLauncher(session)
    sm._availability_checker = _FakeAvailabilityChecker({
        'has_slots': False,
        'glitched_locations': 1,
        'locations_checked': 5,
    })

    await sm._run_check_availability()

    # Only the pre-check ('unknown') should have been consulted.
    assert sm.get_current_state() == MachineState.BACKOFF_WAIT
