"""
Main availability checker - orchestrates OFC location selection, slot
detection, and validation.
"""
from typing import Optional, Dict, Any, List, Tuple
from playwright.async_api import Page
import asyncio
import logging
import random
import re
from datetime import datetime

from app.utils.dom_helpers import DOMHelpers
from app.utils.date_utils import DateUtils
from app.monitor.validator import SlotValidator, ValidationResult
from app.monitor.fingerprint import SlotFingerprint
from app.config import config

logger = logging.getLogger(__name__)


class AvailabilityChecker:
    """
    Checks for slot availability across every configured OFC location,
    with 8-step validation and fingerprinting.

    The portal is a single-page app: picking a location from its
    dropdown doesn't navigate anywhere, it fires an AJAX call that
    replaces a results area with either "No Slots Available" or a
    rendered calendar. This checker drives that dropdown itself, one
    location at a time, and only extracts/validates slots when a
    calendar actually appears - never when a page navigation occurs.
    """

    def __init__(
        self,
        fingerprint_storage_file: Optional[str] = None,
        locations: Optional[List[str]] = None,
        settle_timeout_ms: Optional[int] = None,
        jitter_range_seconds: Optional[Tuple[float, float]] = None,
        selectors_override: Optional[Dict[str, str]] = None,
        interaction_delay_range_seconds: Optional[Tuple[float, float]] = None,
        click_delay_range_ms: Optional[Tuple[int, int]] = None,
    ):
        self._fingerprint = SlotFingerprint(
            storage_file=fingerprint_storage_file or "./data/fingerprints.json"
        )
        self._filters = config.get('monitor.filters', {})
        self._locations = locations if locations is not None else self._filters.get('locations', [])

        # SlotValidator independently validates category/location/
        # appointment_type - pass our own resolved locations through
        # explicitly rather than letting it do its own separate read of
        # monitor.filters, which would silently drift out of sync with
        # this checker's locations whenever they're overridden (e.g. in
        # tests, or if this ever takes a locations override in production).
        self._validator = SlotValidator(filters_override={**self._filters, 'locations': self._locations})

        # selectors_override lets tests point at fixture-appropriate
        # selectors independent of the live config.yaml, which describes
        # the real portal and will keep changing as it's tuned - without
        # this, a test using its own local fixture would silently pick up
        # production selectors that don't match it, fail to find the
        # dropdown, and fall through to a slow (default ~30s) Playwright
        # actionability wait per location instead of failing fast.
        selectors = selectors_override if selectors_override is not None else config.get('portal.selectors', {})
        self._ofc_dropdown_selector = selectors.get(
            'ofc_dropdown', "select#ofc, select[name*='ofc' i], select[id*='location' i]"
        )
        self._results_container_selector = selectors.get(
            'results_container', "#ofcResults, .ofc-results, .schedule-results, [class*='result' i]"
        )
        self._calendar_selector = selectors.get(
            'calendar_container', ".calendar, .datepicker, [class*='calendar' i], table.ui-datepicker-calendar"
        )
        self._no_slots_pattern = selectors.get('no_slots_pattern', r"no\s+slots?\s+available")

        self._settle_timeout_ms = settle_timeout_ms or config.get('portal.ajax.settle_timeout_ms', 15000)
        jitter_min, jitter_max = jitter_range_seconds or (
            config.get('portal.ajax.jitter_min_seconds', 3),
            config.get('portal.ajax.jitter_max_seconds', 7),
        )
        self._jitter_min_seconds = jitter_min
        self._jitter_max_seconds = jitter_max

        # Conservative recovery from portal-side glitches (e.g. a native
        # "PSE0501" alert that leaves the DOM stuck on "Loading...") -
        # only recovers after an entire cycle comes back glitched, and
        # even then only after a long, human-like pause. See
        # _conservative_recovery() for why this is deliberately slow.
        dialog_recovery = config.get('portal.dialog_recovery', {})
        self._min_glitched_cycles_before_reload = dialog_recovery.get('min_glitched_cycles_before_reload', 1)
        self._reload_backoff_min_seconds = dialog_recovery.get('reload_backoff_min_seconds', 45)
        self._reload_backoff_max_seconds = dialog_recovery.get('reload_backoff_max_seconds', 90)
        self._consecutive_glitched_cycles = 0

        # Separate from the counter above (which resets after each
        # recovery attempt, to govern the next attempt's timing): this
        # one only resets on an actual clean cycle, so it can tell "still
        # broken despite recovery attempts" apart from "just glitched
        # once." Crossing the threshold sends one notification, not a
        # repeat every cycle - see check_availability().
        self._notify_after_glitched_cycles = dialog_recovery.get('notify_after_glitched_cycles', 3)
        self._persistent_glitch_streak = 0
        self._persistent_glitch_notified = False

        # Small randomized pauses/click timings around dropdown
        # interaction - not to disguise anything, but so the page's own
        # hover/mousedown/mouseup listeners see a complete interaction
        # sequence rather than an instantaneous value change.
        humanize = config.get('portal.humanize', {})
        self._interaction_delay_min, self._interaction_delay_max = interaction_delay_range_seconds or (
            humanize.get('pre_click_delay_min_seconds', 0.2),
            humanize.get('pre_click_delay_max_seconds', 0.5),
        )
        self._click_delay_min_ms, self._click_delay_max_ms = click_delay_range_ms or (
            humanize.get('click_delay_min_ms', 50),
            humanize.get('click_delay_max_ms', 150),
        )

        # Statistics
        self._stats = {
            'checks_performed': 0,
            'slots_found': 0,
            'valid_slots': 0,
            'duplicate_slots': 0,
            'validation_failures': 0,
            'last_check': None,
            'last_slot_found': None,
        }

    async def check_availability(self, page: Page) -> Dict[str, Any]:
        """
        Cycles through every configured OFC location, in a freshly
        shuffled order each call (so the backend never sees a fixed,
        predictable sequence in its logs): selects each in the dropdown,
        waits for the DOM to settle on either "No Slots Available" or a
        rendered calendar, and only extracts/validates slots when a
        calendar actually appears. A jittered delay separates each
        selection so the backend isn't hammered.
        """
        self._stats['checks_performed'] += 1
        self._stats['last_check'] = datetime.now()

        if not self._locations:
            logger.warning("No locations configured under monitor.filters.locations - nothing to check")
            return {
                'has_slots': False, 'slots': [], 'valid_slots': [], 'duplicate_slots': [],
                'stats': self._stats,
            }

        valid_slots: List[Dict[str, Any]] = []
        duplicate_slots: List[Dict[str, Any]] = []
        all_raw_slots: List[Dict[str, Any]] = []
        location_statuses: Dict[str, str] = {}
        glitched_count = 0

        # A fresh shuffled copy each cycle - self._locations itself (the
        # configured order) is never mutated, since other things (e.g.
        # logging, _is_location_match) may reasonably expect it to stay
        # as configured.
        locations_this_cycle = list(self._locations)
        random.shuffle(locations_this_cycle)

        for i, location in enumerate(locations_this_cycle):
            try:
                calendar_appeared, glitched = await self._select_location_and_wait(page, location)
                if glitched:
                    glitched_count += 1

                if not calendar_appeared:
                    logger.debug(f"{location}: No Slots Available" + (" (glitched)" if glitched else ""))
                    location_statuses[location] = 'Glitched' if glitched else 'No slots'
                else:
                    logger.info(f"{location}: calendar rendered - extracting slots")
                    location_statuses[location] = 'Slots Found'
                    raw_slots = await self._extract_slots(page, location_override=location)
                    all_raw_slots.extend(raw_slots)

                    for slot in raw_slots:
                        if not self._is_location_match(slot):
                            continue

                        validation_result = await self._validator.validate_slot(page, slot)

                        if validation_result.is_valid:
                            fingerprint_result = self._fingerprint.check_and_record(
                                category=slot.get('category', 'H-1B'),
                                location=slot.get('location', location),
                                appointment_type=slot.get('appointment_type', 'Interview'),
                                date=slot.get('date', ''),
                                time=slot.get('time', ''),
                            )

                            # Gate on should_alert (== not already
                            # successfully alerted), not is_new - a slot
                            # seen before but never actually alerted must
                            # still surface, not be dropped forever.
                            if fingerprint_result['should_alert']:
                                valid_slots.append({
                                    'slot': slot,
                                    'validation': validation_result,
                                    'fingerprint': fingerprint_result,
                                })
                                self._stats['valid_slots'] += 1
                                self._stats['last_slot_found'] = datetime.now()
                                logger.info(f"✅ Valid new slot found: {slot}")
                            else:
                                duplicate_slots.append({'slot': slot, 'fingerprint': fingerprint_result})
                                self._stats['duplicate_slots'] += 1
                                logger.debug(f"Duplicate slot (already alerted): {slot}")
                        else:
                            self._stats['validation_failures'] += 1
                            logger.debug(f"Validation failed for {location}: {validation_result.failure_reason}")

            except Exception as e:
                logger.error(f"Error checking location '{location}': {e}", exc_info=True)
                glitched_count += 1
                location_statuses[location] = 'Glitched'
                # Keep going with the remaining locations rather than
                # aborting the whole cycle over one bad location.

            # Jittered pause before the next location so we don't hammer
            # the backend API - skip it after the last location.
            if i < len(locations_this_cycle) - 1:
                jitter = random.uniform(self._jitter_min_seconds, self._jitter_max_seconds)
                logger.debug(f"Waiting {jitter:.1f}s before checking the next location")
                await asyncio.sleep(jitter)

        self._stats['slots_found'] += len(all_raw_slots)

        if self._stats['checks_performed'] % 100 == 0:
            self._fingerprint.cleanup(older_than_days=90)

        # Conservative recovery: only when the ENTIRE cycle glitched (not
        # a single flaky location) do we even consider recovering, and
        # even then only after piling up min_glitched_cycles_before_reload
        # such cycles and a long human-like pause - see
        # _conservative_recovery().
        recovered = False
        needs_attention = False
        attention_reason = None
        all_glitched = glitched_count > 0 and glitched_count == len(locations_this_cycle)

        if all_glitched:
            self._consecutive_glitched_cycles += 1
            self._persistent_glitch_streak += 1
            logger.warning(
                f"All {len(locations_this_cycle)} locations glitched this cycle - "
                f"consecutive glitched cycles: {self._consecutive_glitched_cycles} "
                f"(persistent streak: {self._persistent_glitch_streak})"
            )
            if self._consecutive_glitched_cycles >= self._min_glitched_cycles_before_reload:
                await self._conservative_recovery(page)
                self._consecutive_glitched_cycles = 0
                recovered = True

            # Recovery attempts alone don't clear this - only an actual
            # clean cycle does (see the else branch below) - so this
            # tracks "still broken despite recovery," not just "glitched
            # once." Fires once per bad stretch, not every cycle past
            # the threshold.
            if (self._persistent_glitch_streak >= self._notify_after_glitched_cycles
                    and not self._persistent_glitch_notified):
                needs_attention = True
                attention_reason = (
                    f"The portal has failed to load calendar data for "
                    f"{self._persistent_glitch_streak} consecutive check cycles, despite "
                    f"automatic recovery attempts in between. This looks like a backend-side "
                    f"issue rather than something automatic recovery can fix - you may want "
                    f"to check the portal manually."
                )
                self._persistent_glitch_notified = True
        else:
            self._consecutive_glitched_cycles = 0
            self._persistent_glitch_streak = 0
            self._persistent_glitch_notified = False

        # Reported in configured order, not the shuffled per-cycle visiting
        # order - so the Telegram cycle summary reads consistently every
        # time regardless of this cycle's randomized order.
        ordered_location_statuses = {
            loc: location_statuses.get(loc, 'Not checked') for loc in self._locations
        }

        result = {
            'has_slots': len(valid_slots) > 0,
            'slots': all_raw_slots,
            'valid_slots': valid_slots,
            'duplicate_slots': duplicate_slots,
            'stats': self._stats,
            'timestamp': datetime.now().isoformat(),
            'glitched_locations': glitched_count,
            'locations_checked': len(locations_this_cycle),
            'location_statuses': ordered_location_statuses,
            'recovered': recovered,
            'persistent_glitch_streak': self._persistent_glitch_streak,
            'needs_attention': needs_attention,
            'attention_reason': attention_reason,
        }

        if valid_slots:
            logger.info(f"🎯 Found {len(valid_slots)} new valid slots across all locations!")

        return result

    async def _conservative_recovery(self, page: Page):
        """
        A deliberately slow recovery step - only reached after an entire
        cycle of locations came back glitched, and even then only after a
        long, randomized pause.

        Uses browser history navigation (back, wait, forward) rather than
        page.reload(): reload() issues a fresh request to the exact same
        URL - a same-page-repeated-request pattern some WAFs specifically
        score as suspicious - whereas going back then forward through
        session history is an extremely ordinary browsing action. This
        isn't about disguising anything; it's a different, more
        conservative recovery ACTION, chosen because it empirically
        avoided the Cloudflare challenge that reload() triggered here.
        Falls back to a plain reload only if history navigation isn't
        possible (e.g. no prior entry in this tab).
        """
        wait_time = random.uniform(self._reload_backoff_min_seconds, self._reload_backoff_max_seconds)
        logger.warning(
            f"Entire location cycle glitched - waiting {wait_time:.0f}s (human-like pause) "
            f"before a soft reset (back, then forward, through browser history)"
        )
        await asyncio.sleep(wait_time)

        try:
            previous_url = page.url
            await page.go_back(wait_until='domcontentloaded', timeout=30000)
            if page.url == previous_url:
                raise RuntimeError("go_back() did not change the URL - no previous history entry")

            await asyncio.sleep(random.uniform(2, 4))

            await page.go_forward(wait_until='domcontentloaded', timeout=30000)
            logger.info(f"Soft reset complete (back then forward through history, from {previous_url})")

        except Exception as e:
            logger.warning(f"Soft reset via history navigation failed ({e}) - falling back to a full reload")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=30000)
                logger.info("Page reloaded after repeated glitches (soft-reset fallback)")
            except Exception as e2:
                logger.error(f"Failed to recover page after glitches: {e2}")

    async def _select_location_and_wait(self, page: Page, location: str) -> Tuple[bool, bool]:
        """
        Selects `location` in the OFC dropdown (an SPA/AJAX update, not a
        page navigation) and waits for the DOM to settle on either the
        "No Slots Available" message or a rendered calendar.

        A native browser dialog (alert/confirm) firing during selection -
        e.g. the portal's own "PSE0501" error - would otherwise block all
        further page interaction until dismissed, exactly like it would
        for a real human. A per-attempt listener auto-accepts it (and
        logs the message) so this doesn't hang, same as a human would
        just click through it.

        The previous location's result is cleared from the results
        container first, so a match afterward is guaranteed to be a
        fresh response for the location we're selecting now - not stale
        DOM still sitting there from before this AJAX call completes.

        Returns (calendar_appeared, glitched):
        - calendar_appeared: True if a calendar rendered.
        - glitched: True if a dialog fired, the settle wait timed out
          entirely (stuck on a loading state), or the dropdown selection
          itself failed for any other reason - all of these mean we got
          no real answer for this location, as opposed to a genuine "no
          slots" result. This feeds the conservative recovery decision in
          check_availability(), never triggers recovery by itself.
        """
        dialog_info: Dict[str, Optional[str]] = {'message': None}

        async def handle_dialog(dialog):
            dialog_info['message'] = dialog.message
            logger.warning(
                f"Native dialog appeared while selecting {location} "
                f"({dialog.type}): {dialog.message!r} - auto-accepting"
            )
            try:
                await dialog.accept()
            except Exception as e:
                logger.error(f"Failed to accept dialog for {location}: {e}")

        page.on('dialog', handle_dialog)
        try:
            try:
                cleared = await page.evaluate(
                    """(selector) => {
                        const el = document.querySelector(selector);
                        if (el) { el.innerHTML = ''; return true; }
                        return false;
                    }""",
                    self._results_container_selector,
                )
                if not cleared:
                    # results_container wasn't found for this portal skin -
                    # fall back to clearing just the calendar element.
                    await page.evaluate(
                        """(selector) => {
                            document.querySelectorAll(selector).forEach((el) => el.remove());
                        }""",
                        self._calendar_selector,
                    )
            except Exception as e:
                logger.debug(f"Could not clear previous result before selecting {location} (non-fatal): {e}")

            if not await self._select_dropdown_option(page, location):
                # Any failure to even make the selection - dialog-caused
                # or not (element not found, timeout, etc.) - means we
                # got no real answer for this location. That's a glitch
                # either way, not just when a dialog happened to fire.
                return False, True

            try:
                result_handle = await page.wait_for_function(
                    """({calendarSelector, noSlotsPattern}) => {
                        if (document.querySelector(calendarSelector)) return 'calendar';
                        if (new RegExp(noSlotsPattern, 'i').test(document.body.innerText)) return 'no_slots';
                        return false;
                    }""",
                    arg={'calendarSelector': self._calendar_selector, 'noSlotsPattern': self._no_slots_pattern},
                    timeout=self._settle_timeout_ms,
                )
                outcome = await result_handle.json_value()
                return outcome == 'calendar', dialog_info['message'] is not None
            except Exception:
                # Stuck on "Loading..." (e.g. after a dialog) or the AJAX
                # call never completed - a glitch, not a real "no slots"
                # result, so it's caught here rather than crashing, and
                # tracked separately from a genuine empty result.
                logger.warning(
                    f"{location}: neither 'No Slots Available' nor a calendar appeared "
                    f"within {self._settle_timeout_ms}ms - treating as a glitch, not a real result"
                )
                return False, True
        finally:
            page.remove_listener('dialog', handle_dialog)

    async def _pre_click_pause(self):
        await asyncio.sleep(random.uniform(self._interaction_delay_min, self._interaction_delay_max))

    def _click_delay_ms(self) -> int:
        return random.randint(self._click_delay_min_ms, self._click_delay_max_ms)

    async def _select_dropdown_option(self, page: Page, location: str) -> bool:
        """
        Select `location` in the OFC dropdown. Hovers and clicks the
        element (with a small randomized pause and a real mouse-down/up
        delay) before picking the value, rather than setting it
        instantly - not to disguise anything, but so the page's own
        hover/mousedown/mouseup listeners see the same interaction
        sequence a real click always produces, instead of skipping
        straight to the end state. Tries a native <select> first,
        falling back to a click-driven custom dropdown/combobox widget
        if no native <select> is found - the exact markup can vary by
        portal skin/version, so both are attempted.
        """
        try:
            dropdown = await page.query_selector(self._ofc_dropdown_selector)
            if dropdown:
                tag_name = await dropdown.evaluate('el => el.tagName.toLowerCase()')
                if tag_name == 'select':
                    await dropdown.hover(timeout=5000)
                    await self._pre_click_pause()
                    await dropdown.click(delay=self._click_delay_ms(), timeout=5000)
                    await self._pre_click_pause()
                    await page.select_option(self._ofc_dropdown_selector, label=location, timeout=5000)
                    return True

            # Fallback: a custom (non-native) dropdown widget - open it,
            # then click the option whose text matches the location. An
            # explicit timeout here (rather than Playwright's ~30s
            # default) means a stale/wrong ofc_dropdown selector fails
            # fast instead of stalling every single location check.
            await page.hover(self._ofc_dropdown_selector, timeout=5000)
            await self._pre_click_pause()
            await page.click(self._ofc_dropdown_selector, delay=self._click_delay_ms(), timeout=5000)
            await self._pre_click_pause()

            option = page.locator(f"text={location}").first
            await option.hover(timeout=5000)
            await self._pre_click_pause()
            await option.click(delay=self._click_delay_ms(), timeout=5000)
            return True

        except Exception as e:
            logger.error(f"Failed to select location '{location}' in OFC dropdown: {e}")
            return False

    async def _extract_slots(self, page: Page, location_override: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Extract potential slots from whatever calendar is currently
        rendered.

        `location_override` should be the location just explicitly
        selected in the OFC dropdown - the portal's URL never changes
        between locations, so it can no longer be inferred from the URL.
        """
        slots = []
        resolved_location = location_override or 'Unknown'

        try:
            # Method 1: Extract from date picker
            date_picker_data = await DOMHelpers.get_date_picker_data(page)

            for date_data in date_picker_data:
                date_str = date_data.get('date')
                if date_str and DateUtils.is_valid_date(date_str):
                    slot = {
                        'date': date_str,
                        'category': 'H-1B',  # Default, will be validated
                        'location': resolved_location,
                        'appointment_type': 'Interview',  # Default
                        'time': '',  # Will be filled if available
                        'source': date_data.get('source', 'unknown'),
                        'confidence': date_data.get('confidence', 0.5),
                    }
                    slots.append(slot)

            # Method 2: Look for explicit slot elements
            slot_selectors = [
                '.slot-item.available',
                '.appointment-slot.open',
                '[data-slot="available"]',
                '.slot-card:not(.booked)',
            ]

            for selector in slot_selectors:
                elements = await DOMHelpers.safe_query_selector_all(page, selector)
                for element in elements:
                    slot_info = await self._extract_slot_from_element(element, resolved_location)
                    if slot_info:
                        slots.append(slot_info)

            # Method 3: Check for time slots
            time_slots = await self._extract_time_slots(page)
            if time_slots:
                # If we have time slots, update existing entries
                for time_slot in time_slots:
                    # Try to associate with existing date
                    matched = False
                    for slot in slots:
                        if slot.get('date') == time_slot.get('date'):
                            slot['time'] = time_slot.get('time', '')
                            slot['appointment_type'] = time_slot.get('appointment_type', 'Interview')
                            matched = True
                            break

                    if not matched:
                        # Create new slot entry
                        slots.append({
                            'date': time_slot.get('date', ''),
                            'time': time_slot.get('time', ''),
                            'category': 'H-1B',
                            'location': resolved_location,
                            'appointment_type': time_slot.get('appointment_type', 'Interview'),
                            'source': 'time-slot',
                            'confidence': 0.7,
                        })

            # Remove duplicates based on date+time
            unique_slots = {}
            for slot in slots:
                key = f"{slot.get('date', '')}|{slot.get('time', '')}"
                if key not in unique_slots or unique_slots[key].get('confidence', 0) < slot.get('confidence', 0):
                    unique_slots[key] = slot

            slots = list(unique_slots.values())

        except Exception as e:
            logger.error(f"Error extracting slots for {resolved_location}: {e}")

        return slots

    async def _extract_slot_from_element(
        self, element, location_override: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Extract slot information from a DOM element.
        """
        try:
            # Try to get date from data attributes
            date = await DOMHelpers.get_element_attribute_safe(element, 'data-date')
            if not date:
                # Try to find date in text
                text = await DOMHelpers.get_element_text_safe(element)
                if text:
                    date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})', text)
                    if date_match:
                        date = date_match.group(1)

            if not date or not DateUtils.is_valid_date(date):
                return None

            # Get other attributes
            time = await DOMHelpers.get_element_attribute_safe(element, 'data-time')
            # Prefer the location we explicitly selected in the dropdown
            # over any data-location attribute, which the calendar markup
            # may not even carry (it's implied by the selection instead).
            location = (
                location_override
                or await DOMHelpers.get_element_attribute_safe(element, 'data-location')
                or 'Unknown'
            )

            return {
                'date': date,
                'time': time or '',
                'location': location,
                'category': 'H-1B',
                'appointment_type': 'Interview',
                'source': 'slot-element',
                'confidence': 0.8,
            }

        except Exception as e:
            logger.debug(f"Error extracting slot from element: {e}")
            return None

    async def _extract_time_slots(self, page: Page) -> List[Dict[str, Any]]:
        """
        Extract time slot information.
        """
        time_slots = []

        try:
            time_selectors = [
                '.time-slot',
                '[data-time]',
                '.slot-time',
            ]

            for selector in time_selectors:
                elements = await DOMHelpers.safe_query_selector_all(page, selector)
                for element in elements:
                    time = await DOMHelpers.get_element_attribute_safe(element, 'data-time')
                    if not time:
                        text = await DOMHelpers.get_element_text_safe(element)
                        if text:
                            time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))', text)
                            if time_match:
                                time = time_match.group(1)

                    if time:
                        # Try to find associated date
                        parent = await element.evaluate_handle('el => el.closest("[data-date]")')
                        if parent:
                            date = await parent.get_attribute('data-date')
                        else:
                            date = None

                        time_slots.append({
                            'date': date or '',
                            'time': time,
                            'appointment_type': 'Interview',
                            'source': 'time-slot',
                            'confidence': 0.7,
                        })

        except Exception as e:
            logger.debug(f"Error extracting time slots: {e}")

        return time_slots

    def _is_location_match(self, slot: Dict[str, Any]) -> bool:
        """
        Check if slot location matches configured locations. Mostly a
        defensive check now that the location is set explicitly from the
        dropdown selection rather than inferred, but kept as a cheap
        guard in case a slot ever ends up with a mismatched/missing value.
        """
        if not self._locations:
            return True

        slot_location = slot.get('location', '').lower()
        for location in self._locations:
            if location.lower() in slot_location:
                return True

        return False

    def mark_alerted(self, fingerprint: str) -> bool:
        """
        Mark a fingerprint as successfully alerted, so it won't be
        surfaced again. Call this only after a notification actually went
        out - not before - so a failed send can still retry on the next
        check.
        """
        return self._fingerprint.mark_as_alerted(fingerprint)

    def get_stats(self) -> Dict[str, Any]:
        """
        Get availability checker statistics.
        """
        return {
            **self._stats,
            'fingerprint_stats': self._fingerprint.get_stats(),
        }

    def reset_stats(self):
        """Reset statistics."""
        self._stats = {
            'checks_performed': 0,
            'slots_found': 0,
            'valid_slots': 0,
            'duplicate_slots': 0,
            'validation_failures': 0,
            'last_check': None,
            'last_slot_found': None,
        }
        logger.info("Statistics reset")
