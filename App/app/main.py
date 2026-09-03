"""
Main entry point for H1B Slot Watcher.
"""
import asyncio
import logging

from app.config import config
from app.browser.launcher import BrowserLauncher
from app.monitor.state_machine import StateMachine
from app.logging import setup_logging
from app.safety.kill_switch import KillSwitch
from app.safety.watchdog import Watchdog

logger = logging.getLogger(__name__)


async def _run_one_session(kill_switch: KillSwitch, last_summary: dict):
    """
    Connect to your already-running Chrome (see app/browser/launcher.py -
    this app never launches, logs into, or closes the browser itself)
    and run the state machine to completion. Raises on unexpected crashes
    so the Watchdog can decide whether to reconnect.
    """
    browser_launcher = BrowserLauncher()
    state_machine = StateMachine()

    try:
        await browser_launcher.launch()
        logger.info("Connected to Chrome successfully")

        await state_machine.initialize(browser_launcher)
        await state_machine.run(stop_event=kill_switch.triggered)

    finally:
        await browser_launcher.close()
        last_summary['summary'] = state_machine.get_state_summary()


async def main():
    """Main entry point."""
    setup_logging()

    logger.info("🚀 Starting H1B Slot Watcher...")
    logger.info(f"Config loaded: {config.get('app.name')} v{config.get('app.version')}")

    kill_switch = KillSwitch()
    kill_switch.start()

    last_summary: dict = {}
    watchdog = Watchdog(
        max_restarts=config.get('safety.watchdog_max_restarts', 5),
        window_seconds=config.get('safety.watchdog_window_seconds', 3600),
        restart_backoff=config.get('safety.watchdog_restart_backoff', 10),
    )

    try:
        await watchdog.run(lambda: _run_one_session(kill_switch, last_summary))
    except KeyboardInterrupt:
        logger.info("⚠️ Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
    finally:
        kill_switch.stop()

        summary = last_summary.get('summary')
        if summary:
            logger.info("📊 Final Summary:")
            logger.info(f"  - Total loops: {summary['loop_count']}")
            logger.info(f"  - Final state: {summary['current_state']}")
            logger.info(f"  - Uptime: {summary['uptime_seconds']:.1f} seconds")

        logger.info("👋 Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
