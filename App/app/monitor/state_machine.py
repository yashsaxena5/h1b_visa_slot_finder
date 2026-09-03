"""
State machine for monitoring with explicit transitions.
"""
from enum import Enum
from typing import Optional, Dict, Any, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime
import logging
import asyncio

from app.detection import PortalState, ErrorHandler, ErrorCategory
from app.browser.launcher import BrowserLauncher
from app.monitor.availability import AvailabilityChecker
from app.notifications.manager import AlertManager
from app.safety.backoff import BackoffController

logger = logging.getLogger(__name__)


class MachineState(Enum):
    """All possible states of the monitoring machine."""
    # Normal flow
    INIT = "init"
    SESSION_CHECK = "session_check"
    NAVIGATE = "navigate"
    CHECK_AVAILABILITY = "check_availability"
    VALIDATE = "validate"
    ALERT = "alert"
    PAUSE = "pause"
    
    # Backoff states
    BACKOFF = "backoff"
    BACKOFF_WAIT = "backoff_wait"
    
    # Problem states
    HUMAN_REQUIRED = "human_required"
    MAINTENANCE = "maintenance"
    CAPTCHA = "captcha"
    SESSION_EXPIRED = "session_expired"
    ERROR = "error"
    
    # Terminal
    STOPPED = "stopped"


@dataclass
class Transition:
    """A state transition."""
    from_state: MachineState
    to_state: MachineState
    event: str
    timestamp: datetime = field(default_factory=datetime.now)
    data: Optional[Dict[str, Any]] = None


class StateMachine:
    """
    State machine for monitoring with explicit transitions and error handling.
    """
    
    def __init__(
        self,
        availability_checker: Optional[AvailabilityChecker] = None,
        alert_manager: Optional[AlertManager] = None,
    ):
        self._current_state = MachineState.INIT
        self._previous_state: Optional[MachineState] = None
        self._transitions: list[Transition] = []
        self._state_data: Dict[str, Any] = {}

        # Components (injectable so tests can isolate the fingerprint store
        # and avoid firing real desktop/sound/Telegram notifications)
        self._error_handler = ErrorHandler()
        self._availability_checker = availability_checker or AvailabilityChecker()
        self._alert_manager = alert_manager or AlertManager()
        self._browser_launcher: Optional[BrowserLauncher] = None
        
        # Configuration
        from app.config import config
        self._max_retries = 3
        self._backoff = BackoffController(
            base=config.get('safety.backoff_base', 30),
            max_backoff=config.get('monitor.max_backoff', 3600),
            jitter=config.get('safety.jitter', 5),
        )

        # How many consecutive "on the portal but not yet on a
        # recognized page" checks to tolerate before giving up and
        # escalating to a human - see _run_navigation().
        self._unrecognized_state_streak = 0
        self._max_unrecognized_state_retries = config.get('monitor.max_unrecognized_state_retries', 10)


        # State callbacks
        self._on_state_change_callbacks: list[Callable[[MachineState, MachineState, Optional[Dict]], Awaitable[None]]] = []
        
        # Metrics
        self._loop_count = 0
        self._start_time = datetime.now()
        self._state_durations: Dict[MachineState, float] = {}
        self._last_state_time = datetime.now()
    
    async def initialize(self, browser_launcher: BrowserLauncher):
        """Initialize the state machine with browser launcher."""
        self._browser_launcher = browser_launcher
        self._current_state = MachineState.SESSION_CHECK
        self._last_state_time = datetime.now()
        logger.info("State machine initialized")
    
    async def transition(self, target_state: MachineState, data: Optional[Dict] = None):
        """Transition to a new state."""
        old_state = self._current_state
        self._previous_state = old_state
        self._current_state = target_state
        
        # Record transition
        transition = Transition(old_state, target_state, "state_change", data=data)
        self._transitions.append(transition)
        
        # Track duration of previous state
        now = datetime.now()
        duration = (now - self._last_state_time).total_seconds()
        if old_state in self._state_durations:
            self._state_durations[old_state] += duration
        else:
            self._state_durations[old_state] = duration
        self._last_state_time = now
        
        # Store state data
        if data:
            self._state_data[target_state.value] = data
        
        # Log state change
        logger.info(f"State transition: {old_state.value} → {target_state.value}")
        if data:
            logger.debug(f"State data: {data}")
        
        # Execute callbacks
        for callback in self._on_state_change_callbacks:
            try:
                await callback(old_state, target_state, data)
            except Exception as e:
                logger.error(f"State change callback failed: {e}")
        
        # Trigger special handling for certain states
        await self._handle_state_entry(target_state, data)
    
    async def _handle_state_entry(self, state: MachineState, data: Optional[Dict]):
        """Handle entry into a state."""
        if state == MachineState.ERROR:
            await self._handle_error_state(data)
        elif state == MachineState.HUMAN_REQUIRED:
            await self._handle_human_required(data)
        elif state == MachineState.MAINTENANCE:
            await self._handle_maintenance(data)
        elif state == MachineState.CAPTCHA:
            await self._handle_captcha(data)
        elif state == MachineState.SESSION_EXPIRED:
            await self._handle_session_expired(data)
        elif state == MachineState.BACKOFF:
            await self._handle_backoff(data)
        elif state == MachineState.PAUSE:
            await self._handle_pause(data)

    async def _handle_error_state(self, data: Optional[Dict]):
        """
        Handle error state.

        `data` is expected to carry an 'error_result' key with the dict
        returned by ErrorHandler.handle_error() (which already computed
        requires_human/can_retry correctly) - trust that classification
        instead of re-deriving it here. If no structured error_result is
        present (e.g. an exception escaped a handler that didn't classify
        it), fail safe to HUMAN_REQUIRED rather than silently stalling.
        """
        error_result = data.get('error_result') if data else None

        if not error_result:
            logger.error(f"Entering error state with unclassified error: {data}")
            await self.transition(
                MachineState.HUMAN_REQUIRED,
                {'reason': 'Unclassified error - stopping out of caution', 'data': data}
            )
            return

        category = error_result.get('category')
        category_name = category.value if isinstance(category, ErrorCategory) else str(category)
        requires_human = error_result.get('requires_human', False)
        can_retry = error_result.get('can_retry', False)

        logger.error(
            f"Entering error state: category={category_name}, "
            f"requires_human={requires_human}, can_retry={can_retry}"
        )

        if requires_human:
            await self.transition(
                MachineState.HUMAN_REQUIRED,
                {'reason': f'Unrecoverable error: {category_name}', **(data or {})}
            )
        elif can_retry:
            await self.transition(
                MachineState.BACKOFF,
                {'reason': f'Retryable error: {category_name}'}
            )
        else:
            # Not flagged human-required and out of retries - stop rather
            # than spin, consistent with "when in doubt, halt for a human".
            await self.transition(
                MachineState.HUMAN_REQUIRED,
                {'reason': f'Error with no retry path: {category_name}', **(data or {})}
            )

    async def _handle_captcha(self, data: Optional[Dict]):
        """CAPTCHA must never be auto-solved - halt immediately for a human."""
        logger.critical("🧩 CAPTCHA detected - halting automation. Solve it manually in the open browser.")
        await self.transition(
            MachineState.HUMAN_REQUIRED,
            {'reason': 'CAPTCHA challenge detected', **(data or {})}
        )

    async def _handle_session_expired(self, data: Optional[Dict]):
        """Session/login is no longer valid - halt for a human to re-authenticate."""
        logger.critical("🔑 Session expired or login required - halting automation. Please log in again in the open browser.")
        await self.transition(
            MachineState.HUMAN_REQUIRED,
            {'reason': 'Session expired or login required', **(data or {})}
        )

    async def _handle_human_required(self, data: Optional[Dict]):
        """Handle human required state - the core safety stop."""
        reason = data.get('reason', 'Unknown reason') if data else 'Unknown reason'
        logger.critical(f"🔴 HUMAN INTERVENTION REQUIRED: {reason}")

        if data:
            logger.critical(f"State data: {data}")

        # A silent halt (e.g. session expired while the operator is away)
        # is as bad as a missed slot - let them know, even though this
        # isn't a slot alert.
        try:
            await self._alert_manager.notify_human_required(reason)
        except Exception as e:
            logger.error(f"Failed to send human-required notification: {e}")

    async def _handle_pause(self, data: Optional[Dict]):
        """
        Entered once a slot alert has actually been sent. Per the
        Zero-Auto-Booking invariant, automation does not resume itself
        from here - only a manual restart of the process does.
        """
        reason = data.get('reason', 'Human action required') if data else 'Human action required'
        logger.critical(f"⏸️ AUTOMATION PAUSED: {reason}")
        logger.critical("Take control in the open browser now. Restart the application when ready to resume monitoring.")
    
    async def _handle_maintenance(self, data: Optional[Dict]):
        """
        Portal maintenance is expected/scheduled and not itself a reason to
        wake a human - wait it out and resume automatically.
        """
        from app.config import config
        estimated_end = data.get('estimated_end') if data else None
        wait_time = config.get('monitor.maintenance_backoff', 300)

        if estimated_end:
            logger.info(f"🛠️ Maintenance detected. Estimated end: {estimated_end}. Backing off {wait_time}s.")
        else:
            logger.warning(f"🛠️ Maintenance detected. No estimated end time available. Backing off {wait_time}s.")

        self._state_data['backoff'] = {
            'duration': wait_time,
            'start_time': datetime.now(),
            'reason': 'Portal maintenance window',
        }
        await self.transition(MachineState.BACKOFF_WAIT)
    
    async def _handle_backoff(self, data: Optional[Dict]):
        """
        Log entry into backoff. The actual delay is computed once in
        _run_backoff() (which runs immediately after, in the same loop
        iteration) - this only logs so the specific reason isn't lost.
        """
        reason = data.get('reason', 'Backoff for next check') if data else 'Backoff for next check'
        logger.info(f"🔄 Entering backoff: {reason}")
    
    async def run(self, max_iterations: Optional[int] = None, stop_event: Optional[asyncio.Event] = None):
        """
        Main run loop of the state machine.

        Args:
            max_iterations: Maximum number of iterations (for testing)
            stop_event: an externally-set asyncio.Event (e.g. the global
                Ctrl+Shift+Q hotkey kill switch) that requests an immediate
                clean stop, in addition to the config.yaml `app.enabled`
                kill switch which is checked automatically.
        """
        logger.info("🚀 State machine starting...")

        iteration = 0
        while True:
            iteration += 1
            self._loop_count = iteration
            
            # Check if should stop
            if not self._browser_launcher or not self._browser_launcher.context:
                logger.error("Browser not available")
                break
            
            if max_iterations and iteration > max_iterations:
                logger.info(f"Max iterations reached ({max_iterations})")
                break
            
            # Check kill switches: config.yaml (app.enabled: false) and the
            # global hotkey, if one was wired in.
            from app.config import config
            if not config.enabled:
                logger.info("Kill switch triggered (config.yaml), stopping")
                self.acknowledge_alert()
                await self.transition(MachineState.STOPPED)
                break

            if stop_event is not None and stop_event.is_set():
                logger.info("Kill switch triggered (hotkey), stopping")
                self.acknowledge_alert()
                await self.transition(MachineState.STOPPED)
                break

            # Run current state
            try:
                await self._run_state()
            except Exception as e:
                logger.error(f"Error in state execution: {e}")
                await self.transition(
                    MachineState.ERROR,
                    {'error': str(e), 'exception': e}
                )
            
            # Check if should stop
            if self._current_state == MachineState.STOPPED:
                break
            
            # If in human required, wait for intervention
            if self._current_state == MachineState.HUMAN_REQUIRED:
                logger.info("⏸️ Machine paused, waiting for human intervention")
                await self._interruptible_sleep(60, stop_event)
                continue

            # If in backoff, wait
            if self._current_state == MachineState.BACKOFF_WAIT:
                backoff_data = self._state_data.get('backoff', {})
                wait_time = backoff_data.get('duration', 30)
                logger.info(f"⏳ Waiting {wait_time}s for backoff")
                await self._interruptible_sleep(wait_time, stop_event)

                # Resume after backoff
                await self.transition(MachineState.SESSION_CHECK)
                continue

            # Small delay between loops
            await asyncio.sleep(1)

    async def _interruptible_sleep(self, duration: float, stop_event: Optional[asyncio.Event]):
        """
        Sleep in ~1s increments so a kill switch (config.yaml or the
        global hotkey) can interrupt a long wait promptly instead of
        blocking for the full backoff/pause duration.
        """
        from app.config import config
        remaining = max(0.0, duration)
        while remaining > 0:
            if (stop_event is not None and stop_event.is_set()) or not config.enabled:
                return
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step
    
    async def _run_state(self):
        """Execute the current state."""
        state = self._current_state
        
        if state == MachineState.SESSION_CHECK:
            await self._run_session_check()
        elif state == MachineState.NAVIGATE:
            await self._run_navigation()
        elif state == MachineState.CHECK_AVAILABILITY:
            await self._run_check_availability()
        elif state == MachineState.VALIDATE:
            await self._run_validation()
        elif state == MachineState.ALERT:
            await self._run_alert()
        elif state == MachineState.BACKOFF:
            await self._run_backoff()
        elif state == MachineState.PAUSE:
            await self._run_pause()
        elif state == MachineState.HUMAN_REQUIRED:
            # HUMAN_REQUIRED is meant to persist across many loop
            # iterations while parked, unlike every other state which
            # transitions itself onward within the same _handle_state_entry
            # call. run()'s own HUMAN_REQUIRED check does the actual
            # sleep/park - this only has to be a recognized no-op so the
            # catch-all below doesn't mistake "parked on purpose" for
            # "nobody wrote a handler" and re-fire every iteration.
            pass
        else:
            # Defensive catch-all: an unhandled state must never spin
            # silently - fail safe to HUMAN_REQUIRED so it always surfaces.
            logger.critical(f"State {state.value} has no handler - halting for safety")
            await self.transition(
                MachineState.HUMAN_REQUIRED,
                {'reason': f'Unimplemented state reached: {state.value}'}
            )
    
    async def _route_by_state(self, state_info: Dict[str, Any]) -> bool:
        """
        Route to the correct MachineState based on a freshly-detected page
        state. Shared by session-check and post-navigation checks so a
        CAPTCHA/maintenance/session-expiry/access-denied page is always
        triaged the same way no matter where it's spotted.

        Returns True if a transition was made (caller should stop), False
        if the state is normal/expected and the caller should proceed.
        """
        state = state_info.get('state')

        if state == PortalState.MAINTENANCE.value:
            await self.transition(MachineState.MAINTENANCE, state_info)
            return True
        if state == PortalState.CAPTCHA.value:
            await self.transition(MachineState.CAPTCHA, state_info)
            return True
        if state in (PortalState.SESSION_EXPIRED.value, PortalState.LOGIN_REQUIRED.value):
            await self.transition(MachineState.SESSION_EXPIRED, state_info)
            return True
        if state == PortalState.ACCESS_DENIED.value:
            await self.transition(
                MachineState.HUMAN_REQUIRED,
                {**state_info, 'reason': 'Access denied'}
            )
            return True

        return False

    async def _run_session_check(self):
        """Check if session/page is in a safe, usable state."""
        logger.debug("Checking session...")

        try:
            is_valid = await self._browser_launcher.session.is_session_valid()

            if is_valid:
                await self.transition(MachineState.NAVIGATE)
                return

            state_info = await self._browser_launcher.session.detect_page_state()
            if await self._route_by_state(state_info):
                return

            # Invalid session but not one of the specifically-recognized
            # problem states - stop for a human rather than guess.
            await self.transition(MachineState.HUMAN_REQUIRED, state_info)

        except Exception as e:
            error_result = await self._error_handler.handle_error(e, {'action': 'session_check'})
            await self.transition(
                MachineState.ERROR,
                {'error_result': error_result, 'action': 'session_check'}
            )

    def _is_on_allowed_domain(self, state_info: Dict[str, Any]) -> bool:
        """Whether the state_info's URL is on one of portal.allowed_domains."""
        from app.config import config
        allowed_domains = config.get('portal.allowed_domains', [])
        url = (state_info.get('url') or '').lower()
        return any(domain.lower() in url for domain in allowed_domains)

    async def _run_navigation(self):
        """Navigate to the portal."""
        logger.debug("Navigating to portal...")

        try:
            success = await self._browser_launcher.session.navigate_to_portal()

            if not success:
                await self.transition(MachineState.BACKOFF, {'reason': 'Navigation failed'})
                return

            state_info = await self._browser_launcher.session.detect_page_state()
            if await self._route_by_state(state_info):
                return

            if state_info.get('state') in ('dashboard', 'appointment', 'scheduling'):
                self._unrecognized_state_streak = 0
                await self.transition(MachineState.CHECK_AVAILABILITY)
                return

            if self._is_on_allowed_domain(state_info):
                # Still on the portal itself - most likely you haven't
                # navigated to the exact scheduling page yet (e.g. the
                # bot started on the homepage), or the page just hasn't
                # rendered the DOM markers CHECK_AVAILABILITY looks for.
                # That's not a problem worth waking a human for - wait
                # the normal poll interval and check again, up to a bound
                # so a *genuinely* stuck state still eventually surfaces.
                self._unrecognized_state_streak += 1
                if self._unrecognized_state_streak > self._max_unrecognized_state_retries:
                    await self.transition(
                        MachineState.HUMAN_REQUIRED,
                        {
                            **state_info,
                            'reason': (
                                f"Still not on a recognized scheduling page after "
                                f"{self._unrecognized_state_streak} checks"
                            ),
                        }
                    )
                    return

                from app.config import config
                logger.info(
                    f"On the portal but not on a recognized page yet "
                    f"(state={state_info.get('state')}) - waiting and checking again "
                    f"[{self._unrecognized_state_streak}/{self._max_unrecognized_state_retries}]"
                )
                self._state_data['backoff'] = {
                    'duration': config.get('monitor.check_interval', 30),
                    'start_time': datetime.now(),
                    'reason': 'Waiting for the scheduling page to appear',
                }
                await self.transition(MachineState.BACKOFF_WAIT)
            else:
                # Not even on the portal domain - something else is
                # going on (redirected away, wrong tab, etc.) - this one
                # does warrant a human's attention.
                await self.transition(
                    MachineState.HUMAN_REQUIRED,
                    {**state_info, 'reason': 'Unexpected page state - not on the portal domain'}
                )

        except Exception as e:
            error_result = await self._error_handler.handle_error(e, {'action': 'navigation'})
            await self.transition(
                MachineState.ERROR,
                {'error_result': error_result, 'action': 'navigation'}
            )
    
    async def _run_check_availability(self):
        """Check for available slots (extraction + the 8-step validation + fingerprinting)."""
        logger.debug("Checking availability...")

        try:
            # CHECK_AVAILABILITY can loop for many cycles in a row without
            # NAVIGATE ever running again to catch a problem state - and a
            # block/CAPTCHA/session-expiry page appearing mid-loop must
            # never be ground through as just another dropdown "glitch"
            # to recover from. Re-verify before driving the dropdown.
            state_info = await self._browser_launcher.session.detect_page_state()
            if await self._route_by_state(state_info):
                return

            page = await self._browser_launcher.session.get_page()
            result = await self._availability_checker.check_availability(page)

            # A cycle where every location failed is exactly the symptom
            # of the page having changed out from under us mid-loop (e.g.
            # to an access-denied page) - re-check state immediately
            # rather than letting the glitch/recovery machinery (built
            # for transient hiccups) keep grinding against a page that's
            # explicitly telling us to stop.
            glitched = result.get('glitched_locations', 0)
            checked = result.get('locations_checked', 0)
            if checked and glitched == checked:
                post_state_info = await self._browser_launcher.session.detect_page_state(force_refresh=True)
                if await self._route_by_state(post_state_info):
                    return

            # A persistent glitch streak (e.g. repeated "PSE0501" errors
            # that automatic recovery keeps failing to clear) is worth
            # telling you about even though it doesn't halt automation -
            # unlike CAPTCHA/session-expiry, there's nothing for a human
            # to unblock here, recovery keeps retrying on its own, but a
            # silent multi-cycle backend outage shouldn't be invisible.
            if result.get('needs_attention'):
                try:
                    await self._alert_manager.notify_human_required(result.get('attention_reason', 'Repeated availability-check glitches'))
                except Exception as e:
                    logger.error(f"Failed to send persistent-glitch notice: {e}")

            # Routine telemetry so you know the bot is alive and what it
            # saw without watching the screen - independently throttled
            # inside AlertManager, so calling this every cycle is safe
            # even at the fast steady-state poll interval.
            try:
                await self._alert_manager.notify_cycle_summary(result.get('location_statuses', {}))
            except Exception as e:
                logger.error(f"Failed to send cycle summary: {e}")

            if result.get('has_slots'):
                await self.transition(MachineState.VALIDATE, result)
            else:
                # A clean check with nothing found is the expected steady
                # state, not an error - wait the normal poll interval and
                # reset any error-driven backoff, rather than routing
                # through BACKOFF's exponential growth (which is reserved
                # for actual transient errors/server lag).
                from app.config import config
                self._backoff.reset()
                self._state_data['backoff'] = {
                    'duration': config.get('monitor.check_interval', 30),
                    'start_time': datetime.now(),
                    'reason': 'No new slots found - normal poll interval',
                }
                await self.transition(MachineState.BACKOFF_WAIT)

        except Exception as e:
            error_result = await self._error_handler.handle_error(e, {'action': 'check_availability'})
            await self.transition(
                MachineState.ERROR,
                {'error_result': error_result, 'action': 'check_availability'}
            )

    async def _run_validation(self):
        """
        The 8-step validation and SHA-256 fingerprint dedup already ran
        inside AvailabilityChecker.check_availability() - this state exists
        purely for transition-history/audit visibility, matching the
        documented NORMAL -> ... -> VALIDATE -> ALERT flow.
        """
        logger.debug("Slots already validated by the availability checker - proceeding to alert.")
        result = self._state_data.get(MachineState.VALIDATE.value, {})
        await self.transition(MachineState.ALERT, result)

    async def _run_alert(self):
        """Send the three-layer alert for every newly-confirmed slot, then pause."""
        result = self._state_data.get(MachineState.ALERT.value, {})
        valid_slots = result.get('valid_slots', [])

        if not valid_slots:
            logger.warning("Entered ALERT state with no valid slots - treating as a no-op")
            await self.transition(MachineState.BACKOFF, {'reason': 'Alert state with no slots'})
            return

        for entry in valid_slots:
            slot = entry['slot']
            fingerprint = entry['fingerprint']['fingerprint']
            try:
                await self._alert_manager.send_slot_alert(slot, fingerprint)
                # Only mark as alerted after a real send attempt completed -
                # if this line is never reached (exception above), the slot
                # stays eligible to be retried on the next check.
                self._availability_checker.mark_alerted(fingerprint)
            except Exception as e:
                logger.error(f"Failed to send alert for slot {slot}: {e}")

        await self.transition(
            MachineState.PAUSE,
            {'reason': 'Slot alert sent - awaiting human action in the browser', 'slots': valid_slots}
        )
    
    async def _run_backoff(self):
        """Compute the real exponential+jittered delay and move to BACKOFF_WAIT."""
        existing = self._state_data.get('backoff') or {}
        reason = existing.get('reason', 'Transient condition')
        backoff_time = self._backoff.next_delay()

        self._state_data['backoff'] = {
            'duration': backoff_time,
            'start_time': datetime.now(),
            'reason': reason,
        }

        await self.transition(MachineState.BACKOFF_WAIT)
    
    async def _run_pause(self):
        """
        Stay paused. The critical log/notification already happened once
        in _handle_pause() on entry - this just idles without busy-waiting,
        re-checking the kill switch each loop tick via run().
        """
        await asyncio.sleep(5)
    
    # Public methods

    def acknowledge_alert(self):
        """Stop any active sound alarm - call when the human takes over (e.g. kill switch)."""
        self._alert_manager.acknowledge()

    def get_current_state(self) -> MachineState:
        """Get current state."""
        return self._current_state
    
    def get_transition_history(self) -> list[Transition]:
        """Get transition history."""
        return self._transitions
    
    def get_state_summary(self) -> Dict[str, Any]:
        """Get summary of state machine."""
        return {
            'current_state': self._current_state.value,
            'previous_state': self._previous_state.value if self._previous_state else None,
            'loop_count': self._loop_count,
            'total_transitions': len(self._transitions),
            'start_time': self._start_time.isoformat(),
            'uptime_seconds': (datetime.now() - self._start_time).total_seconds(),
            'state_durations': {k.value: v for k, v in self._state_durations.items()},
            'backoff': self._state_data.get('backoff'),
            'error_stats': self._error_handler.get_error_stats(),
        }
    
    def add_state_change_callback(self, callback: Callable[[MachineState, MachineState, Optional[Dict]], Awaitable[None]]):
        """Add callback for state changes."""
        self._on_state_change_callbacks.append(callback)
    
    async def stop(self):
        """Stop the state machine."""
        await self.transition(MachineState.STOPPED, {'reason': 'Manual stop'})
        logger.info("State machine stopped")
    
    def reset_backoff(self):
        """Reset backoff timer."""
        self._backoff.reset()
        logger.debug("Backoff reset")