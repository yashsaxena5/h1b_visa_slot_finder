# H1B Slot Watcher

A high-reliability, human-in-the-loop monitor for H-1B visa appointment
slots on `usvisascheduling.com` (CGI Federal). It watches for a matching
slot, verifies it thoroughly, and alerts a human to book it manually. It
never books, pays, or submits anything itself.

## Core rule: Zero Auto-Booking

**Bot detects → bot verifies → bot alerts → automation pauses → human books manually.**

- No automatic appointment submission or payment, ever.
- No CAPTCHA solvers or bypasses - a CAPTCHA halts the automation immediately.
- No plaintext credential storage - login happens once, manually, in a real browser window.
- No undocumented private API calls - everything goes through the same browser UI a human would use.

## How it works

```
SESSION_CHECK → NAVIGATE → CHECK_AVAILABILITY → VALIDATE → ALERT → PAUSE
      │              │
      ├─ CAPTCHA / MAINTENANCE / SESSION_EXPIRED / ACCESS_DENIED
      │       └─ CAPTCHA, SESSION_EXPIRED, ACCESS_DENIED → HUMAN_REQUIRED (halt, notify, wait)
      │       └─ MAINTENANCE → BACKOFF_WAIT (expected/scheduled, resumes on its own)
      └─ transient error → BACKOFF (exponential: 30s, 60s, 120s, ... up to 1h)
```

A candidate slot must pass an 8-step check (category, location, appointment
type, DOM selectability of the date, DOM selectability of the time, fresh
(non-cached) server state, still exists on re-verification) plus a SHA-256
fingerprint dedup check, before it is ever surfaced. Once alerted, a
fingerprint is never re-alerted - but a slot that was *seen* but never
successfully *alerted* (e.g. Telegram was down) will still be retried on
the next check, rather than being silently dropped forever.

When a slot clears all checks, three independent, isolated alert layers fire:

1. **Telegram** - exact slot details, then a short secondary "urgent" follow-up.
2. **Desktop notification** - native OS popup (macOS `osascript`, Linux `notify-send`).
3. **Sound alarm** - repeats every few seconds for a bounded window (default ~2 min), then auto-stops so it can't ring forever unattended.

The automation then pauses indefinitely - only restarting the process resumes monitoring.

Non-slot halts (session expired, CAPTCHA, an unrecoverable error) also send
a lighter Telegram + desktop notice, so a silent stop while you're away is
never invisible - but they do not trigger the sound alarm, which is
reserved for confirmed slots so it doesn't lose urgency from firing on
every hiccup.

## Setup

1. **Install dependencies**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   (Chromium is only needed for the test suite's throwaway browser
   instances - the app itself connects to your real Chrome, see below.)

2. **Configure Telegram (optional but recommended)**

   Copy `.env.example` to `.env` and fill in a bot token + chat ID:

   ```bash
   cp .env.example .env
   ```

   Create a bot via [@BotFather](https://t.me/BotFather), message it once,
   then visit `https://api.telegram.org/bot<token>/getUpdates` to find your
   `chat_id`. `.env` is gitignored - it is never committed.

3. **Launch your own Chrome first, every time**

   This app connects to a real, normal Google Chrome window you start
   yourself - it never launches or manages the browser process itself:

   ```bash
   # macOS
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 \
     --user-data-dir="$(pwd)/browser_profile"

   # Linux
   google-chrome --remote-debugging-port=9222 --user-data-dir="$(pwd)/browser_profile"
   ```

   `--user-data-dir=./browser_profile` keeps your login persistent across
   runs in the same conventional location this project has always used -
   it's a completely normal Chrome profile directory, so anything already
   in there from before still works.

   The first time, log in manually in that window, solve any CAPTCHA
   yourself, and navigate to the scheduling screen. Leave the window open.

4. **Run the app** in a separate terminal:

   ```bash
   python3 -m app.main
   ```

   It connects to the Chrome window from step 3 over the DevTools
   protocol (`browser.cdp_url` in `config/config.yaml`, default
   `http://localhost:9222`) and opens a new tab in it for monitoring.
   Because Chrome wasn't launched with Playwright's own `--enable-automation`
   flag, `navigator.webdriver` is never set and no "controlled by automated
   test software" banner appears - there's nothing to mask, because the
   signal that used to trip Cloudflare is simply never present in the
   first place.

   If a CAPTCHA still appears, solve it yourself in that same window -
   the state machine halts and waits for exactly that (see `HUMAN_REQUIRED`
   below).

   It's fine to start the app before navigating to the exact scheduling
   page (`/ofc-schedule/`) - being on the portal domain but not yet on a
   recognized page just waits and re-checks at the normal poll interval
   (`monitor.check_interval`) rather than halting, for up to
   `monitor.max_unrecognized_state_retries` checks (default 10, ~5
   minutes) before it gives up and asks for a human. Navigate there
   yourself whenever you're ready; the next check picks it up.

5. **Every subsequent run**: make sure the Chrome from step 3 is still
   running (or relaunch it the same way), then re-run `python3 -m app.main`.
   Closing the app (`Ctrl+C`, the kill switch, or the hotkey) never closes
   your Chrome window - only your own terminal/window controls do that.

## The OFC location dropdown (SPA/AJAX)

The scheduling page doesn't navigate when you pick a city - it's a
single-page app: selecting an OFC center fires an AJAX call that renders
either "No Slots Available" or a calendar, with the URL staying exactly
the same. `AvailabilityChecker` drives that dropdown itself: for each
location in `monitor.filters.locations`, in a freshly shuffled order every
cycle (so the backend never sees a fixed, predictable sequence), it
selects the option, waits for the DOM to settle on one of those two
outcomes, and only extracts/validates slots when a calendar actually
appears - moving to the next location with a random 3-7s pause
(`portal.ajax.jitter_*_seconds`) so the backend isn't hammered.

The four selectors this depends on (`portal.selectors.ofc_dropdown`,
`results_container`, `calendar_container`, `no_slots_pattern` in
`config/config.yaml`) ship with best-guess defaults, since the exact
markup varies by portal skin/version. **Verify them against the real
page**: open the scheduling page in DevTools, right-click the dropdown /
the results area / the calendar / the "No Slots Available" text, and
"Copy selector" for each - update the config with whatever you find.

### Native dialog / portal glitch recovery

The portal occasionally throws a native browser alert (e.g. an
"error code PSE0501" popup) when selecting a location, which otherwise
blocks all further interaction until dismissed - exactly like it would
for a human. Every selection attempt has its own dialog listener that
auto-accepts it (and logs the message), the same thing you'd do by hand.

If an entire cycle of locations comes back glitched (dialog or stuck on
"Loading..."), the checker does **not** reload immediately. It waits a
randomized human-like pause (`portal.dialog_recovery.reload_backoff_min/
max_seconds`, default 45-90s) and only recovers after
`min_glitched_cycles_before_reload` such cycles in a row (default 1).
Recovery goes back through browser history, waits, then forward again -
not `page.reload()`, which issues a fresh request to the exact same URL
(a pattern that, empirically, tripped a Cloudflare challenge here; back/
forward through history didn't). It falls back to a plain reload only if
there's no history to go back to. Either way, repeatedly hammering a
WAF-protected page in response to a transient glitch is exactly the
aggressive automated behavior this project exists to avoid - a real user
hitting the same glitch would also just wait a bit before doing anything.

If glitches persist across `portal.dialog_recovery.notify_after_glitched_
cycles` cycles (default 3) *despite* recovery attempts in between, you get
one Telegram/desktop notice saying so - this doesn't halt automation
(recovery keeps retrying on its own; there's nothing to unblock the way
a CAPTCHA needs you to), it just makes sure a multi-cycle backend outage
isn't invisible. Fires once per bad stretch, not every cycle past the
threshold.

Each dropdown selection also hovers and clicks with a small randomized
delay (`portal.humanize.*`) before picking the value, instead of setting
it instantly - not to disguise the automation, but so the page's own
hover/mousedown/mouseup listeners see the complete interaction sequence
a real click always produces rather than skipping straight to the end
state.

### If the portal blocks access outright

Separately from transient glitches, the portal can serve an explicit
block page ("Access limitation ... Prohibited conduct") if it decides
this account's access violates its terms. `CHECK_AVAILABILITY` re-checks
page state (the same `access_denied`/CAPTCHA/session-expiry detection
NAVIGATE uses) both before starting a cycle and immediately after any
cycle where every location failed - the dropdown simply disappearing is
exactly what a block page looks like from `AvailabilityChecker`'s point
of view, and that must never be treated as just another glitch to
recover from. Either check halts straight to `HUMAN_REQUIRED`, bypassing
the glitch-recovery machinery entirely - that machinery exists for
transient hiccups, not for "the site told us to stop."

## Configuration (`config/config.yaml`)

| Key | Purpose |
|---|---|
| `app.enabled` | Global kill switch - flip to `false` (even while running) to stop cleanly. Picked up live via a file-mtime check, no restart needed. |
| `browser.cdp_url` | Where to connect for the Chrome you launched yourself (default `http://localhost:9222`). |
| `monitor.check_interval` | Normal poll interval once nothing's wrong. |
| `monitor.max_backoff` | Ceiling for exponential backoff on transient errors. |
| `monitor.maintenance_backoff` | Fixed wait during a detected maintenance window. |
| `monitor.max_unrecognized_state_retries` | Checks to tolerate being on the portal but not yet on a recognized page (e.g. still on the homepage) before escalating to a human. |
| `monitor.filters.*` | Category / OFC location allow-list (also drives which locations get cycled through in the dropdown) / appointment type allow-list. |
| `portal.selectors.ofc_dropdown` / `results_container` / `calendar_container` / `no_slots_pattern` | The OFC dropdown SPA selectors - see above, verify against the real page. |
| `portal.ajax.settle_timeout_ms` | Max wait for the AJAX response after picking a location. |
| `portal.ajax.jitter_min_seconds` / `jitter_max_seconds` | Random pause range between locations. |
| `portal.dialog_recovery.*` | How many fully-glitched cycles to tolerate, and the human-like pause range, before a single conservative recovery (back/forward through history, reload as fallback). `notify_after_glitched_cycles` controls the separate "tell me it's still broken" notice. |
| `portal.humanize.*` | Pre-click pause range and click mouse-down/up delay range for dropdown interaction. |
| `notifications.telegram.*` | Bot enable flag + rate limit (messages/minute). Token/chat ID come from `.env`. |
| `notifications.sound.*` | Alarm file, repeat interval, and max repeat count before auto-stop. |
| `safety.kill_switch_hotkey` | Global hotkey (default `Ctrl+Shift+Q`) that stops immediately from anywhere. |
| `safety.watchdog_*` | How many browser-crash restarts are allowed within what window before the watchdog gives up. |

## Safety mechanisms

- **Config kill switch**: set `app.enabled: false` in `config/config.yaml` at any time; it's re-read live, no restart needed.
- **Hotkey kill switch**: `Ctrl+Shift+Q` (configurable) stops immediately from anywhere, and silences any active alarm. On macOS this requires granting Accessibility permission to the terminal/process running it - if that's not granted, the hotkey just won't arm (logged as a warning) and the config switch still works.
- **Watchdog**: if the CDP connection drops, it retries connecting to the same `browser.cdp_url` (your Chrome window, if it's still running), with a bounded number of attempts so a persistent underlying problem doesn't restart-loop forever. It never launches, logs into, or closes your browser - that's entirely yours to control.
- **Fail-safe state routing**: every code path that can't classify what's happening halts to `HUMAN_REQUIRED` rather than silently retrying forever.
- **No automation fingerprint to hide**: because Chrome is launched by you, not by Playwright, it never gets Playwright's own `--enable-automation` flag - `navigator.webdriver` is simply never set. This project does not use `playwright-stealth` or any other detection-evasion library, and won't: a CAPTCHA is meant to halt the automation and hand control to you, not be defeated programmatically.

## Running tests

```bash
pytest
```

All tests run entirely offline against local HTML fixtures in
`tests/fixtures/` (loaded via Playwright's `page.set_content()`) - none of
them navigate to the live portal. Repeatedly driving an automated browser
against a WAF-protected site from a test suite is exactly the kind of
bot-detection/rate-limit risk this project exists to avoid.

Tests that exercise `BrowserLauncher` don't require you to have your own
Chrome running: a `cdp_browser_launcher` fixture (see `tests/conftest.py`)
launches a throwaway headless Chromium with its own debug port to stand
in for "the user's manually-launched Chrome," so the real
`connect_over_cdp()` path is genuinely exercised, unattended.

- `tests/test_detection.py` - portal state detection (dashboard/CAPTCHA/maintenance/session-expiry, including Cloudflare's actual "Just a moment..." interstitial) and the state-cache TTL.
- `tests/test_validator.py` - the 8-step slot validation, including a deliberately "looks available but is disabled" fixture to confirm false positives are rejected.
- `tests/test_availability.py` - the OFC dropdown/AJAX cycling flow against a fixture that simulates the real SPA behavior, including regression tests for the stale-DOM race between locations, native-dialog auto-accept, and the conservative reload trigger/threshold. Note: `AvailabilityChecker` accepts `selectors_override`/`filters_override` (via the `SlotValidator` it builds) specifically so tests never silently pick up the *real* portal's selectors/location names from the live `config.yaml` - without that, a test using its own fixture would fail confusingly or hang on Playwright's default action timeout hunting for a selector that only exists on the real site.
- `tests/test_state_machine.py` - state-routing/transition logic, including regression tests for the CAPTCHA-bypass, error-classification, unhandled-state, and `HUMAN_REQUIRED`-dispatch bugs this codebase was fixed for.
- `tests/test_launcher.py`, `tests/test_m2_integration.py` - integration tests against a real (CDP-connected) browser, with navigation stubbed to local fixtures, including a regression test for the navigate-when-already-there fix.

## Project layout

```
app/
├── main.py                 # entry point: kill switch + watchdog + state machine
├── config.py                # YAML config + .env substitution + live kill-switch reload
├── browser/
│   ├── launcher.py           # connects to your own Chrome over CDP (never launches it)
│   └── session.py            # page lifecycle, state detection entry point
├── detection/
│   ├── state_detector.py     # pattern/DOM-based portal state classification (TTL-cached)
│   ├── error_handler.py      # exception -> category -> retry/human-required classification
│   ├── session_expiry.py     # session validity signals
│   └── maintenance.py        # standalone maintenance-window helper (not yet wired in)
├── monitor/
│   ├── state_machine.py      # the state machine described above
│   ├── availability.py       # slot extraction + orchestrates validation + fingerprinting
│   ├── validator.py           # the 8-step check
│   └── fingerprint.py         # SHA-256 dedup store
├── notifications/
│   ├── telegram.py, desktop.py, sound.py, manager.py   # the three alert layers
├── safety/
│   ├── backoff.py, rate_limiter.py, kill_switch.py, watchdog.py
└── utils/
    ├── date_utils.py, dom_helpers.py

config/config.yaml   # all tunables
tests/                # offline unit + integration tests, fixtures/ for mock HTML
```
