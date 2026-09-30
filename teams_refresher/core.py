"""The watch loop, with nothing OS-specific left in it.

The original daemon was event-driven: it asked Mutter to wake it the moment
the idle clock crossed the threshold. Windows and macOS have no equivalent to
subscribe to, so the loop here sleeps instead -- but it sleeps until exactly
the moment the threshold *could* next be crossed:

    idle 12s, threshold 180s  ->  nothing can happen for 168s, so sleep 168s

That costs one cheap idle query per wake, and while you are working it
naturally backs off to nearly nothing. It is not literally event-driven, but
it wakes about as rarely, and the same code now runs on all three systems.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .backends.base import IdleMonitor, Injector, LockMonitor

LOG = logging.getLogger("teams-refresher")

#: Never block longer than this in one go, so Ctrl-C stays responsive and a
#: suspended laptop re-checks promptly on resume.
MAX_WAIT = 60.0
#: How often to look again while the screen is locked.
LOCKED_POLL = 10.0
#: Never sleep less than this, however the arithmetic comes out.
MIN_TICK = 1.0
#: Let the injected event travel through the input stack before re-reading.
VERIFY_DELAY = 0.3


class Refresher:
    def __init__(
        self,
        threshold: float,
        injector: Injector,
        idle_monitor: IdleMonitor,
        lock_monitor: LockMonitor,
        allow_locked: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.threshold = float(threshold)
        self.injector = injector
        self.idle = idle_monitor
        self.lock = lock_monitor
        self.allow_locked = allow_locked
        self.dry_run = dry_run

        # Hard floor between nudges, so a nudge that fails to reset the clock
        # cannot turn the loop into a spin.
        self.cooldown = max(5.0, self.threshold / 10)
        self.nudges = 0
        self._last_nudge = float("-inf")
        self._ineffective = 0
        self._was_locked: Optional[bool] = None
        self._stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to finish. Safe to call from a signal handler."""
        self._stop.set()

    def run(self) -> int:
        LOG.info(
            "watching: nudge after %gs idle, key=%s, idle clock=%s, lock=%s, "
            "locked-screen=%s%s",
            self.threshold, self.injector.key_name, self.idle.name, self.lock.name,
            "active" if self.allow_locked else "paused",
            ", DRY RUN" if self.dry_run else "",
        )
        while not self._stop.is_set():
            self._wait(self._tick())
        LOG.info("stopped after %d nudge(s)", self.nudges)
        return 0

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, MAX_WAIT))

    # -- one decision -------------------------------------------------------

    def _tick(self) -> float:
        """Decide whether to nudge, and return how long to sleep afterwards."""
        locked = self.lock.is_locked()
        if locked is not None and locked != self._was_locked:
            if self._was_locked is not None or locked:
                LOG.info(
                    "screen %s -- %s",
                    "locked" if locked else "unlocked",
                    "pausing" if locked and not self.allow_locked else "active",
                )
            self._was_locked = locked
        if locked and not self.allow_locked:
            return LOCKED_POLL

        idle = self.idle.idle_seconds()
        if idle is None:
            # No idle clock here: fall back to nudging on a fixed cadence.
            # Harmless, just less polite than waiting for real inactivity.
            self._nudge("blind timer")
            return self.threshold

        if idle < self.threshold:
            # Real activity, or a nudge that landed. Sleep until the earliest
            # moment the threshold could be reached.
            return _clamp(self.threshold - idle, MIN_TICK, self.threshold)

        waited = time.monotonic() - self._last_nudge
        if waited < self.cooldown:
            return _clamp(self.cooldown - waited, MIN_TICK, self.cooldown)

        if not self._nudge("idle %.0fs" % idle):
            return self.cooldown
        return self._verify()

    def _nudge(self, reason: str) -> bool:
        self._last_nudge = time.monotonic()
        self.nudges += 1
        if self.dry_run:
            LOG.info("[dry-run] would nudge #%d (%s)", self.nudges, reason)
            return True
        try:
            self.injector.nudge()
        except OSError as exc:
            LOG.error("failed to emit nudge: %s -- reopening the device", exc)
            self._reopen()
            return False
        # Deliberately does not claim the clock reset: _verify decides that,
        # and in blind mode nothing can.
        LOG.info("nudge #%d sent (%s) -- %s tapped",
                 self.nudges, reason, self.injector.key_name)
        return True

    def _verify(self) -> float:
        """Confirm the nudge actually moved the clock; complain if it didn't.

        This is the daemon's own smoke test. Silently doing nothing is the
        one failure mode that would otherwise look exactly like working.
        """
        if self.dry_run:
            return self.cooldown
        self._stop.wait(VERIFY_DELAY)
        idle = self.idle.idle_seconds()
        if idle is None:
            return self.threshold
        if idle >= self.threshold:
            self._ineffective += 1
            if self._ineffective in (1, 5, 20):
                LOG.warning(
                    "nudge did not reset the idle clock (still %.0fs) -- %dx now. "
                    "The synthetic input is not reaching the idle clock; check "
                    "the permissions listed by --status, or try --pointer.",
                    idle, self._ineffective,
                )
            return self.cooldown
        if self._ineffective:
            LOG.info("idle clock responding again")
            self._ineffective = 0
        return _clamp(self.threshold - idle, MIN_TICK, self.threshold)

    def _reopen(self) -> None:
        try:
            self.injector.close()
            self.injector.open()
        except Exception as exc:  # noqa: BLE001 - last-resort recovery
            LOG.error("could not reopen the injector: %s", exc)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
